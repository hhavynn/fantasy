# Situational usage splits and RB workload components (ifp-su, research only)

Date: 2026-10-18. Status: **research only.** Nothing here is promoted, read by a consumer, or changes
`ifp-1.0.0`, `ifp-2.0.0`, the model registry, any artifact or any forecast. Code:
`backend/app/analytics/situational_usage.py`, `scripts/situational_usage_research.py`, tests
`backend/tests/test_situational_usage.py`.

> **Status of this file.** Sections 1-4 (prior art, data, development-window feature diagnostics, preregistration)
> were committed *before* any candidate was fitted or scored. Sections 5-8 are added afterwards, in later commits,
> and never edit the preregistration block (the scripts hash it: `develop` records the hash and `confirm` refuses
> to run if it changed).

**Question.** Do situational usage splits (goal-line share, down-and-distance splits, two-minute usage,
game-script-conditioned usage, opportunity per snap) predict next-week workload and fantasy points better than the
total-volume features ifp-2 already uses? Especially for RBs: does projecting the components separately
(carries, inside-5 carries, targets) and recombining them beat ifp-2's direct ridge, and does it separate
passing-down backs from early-down backs who depend on a positive script?

This is the "situational usage" workstream of the NFL Savant review: Savant-style metrics are **re-derived from
nflverse play-by-play we already ingest**; no NFL Savant data is read and Savant is not a dependency.

## 1. What already exists (and what this study does not repeat)

| Already in ifp-2 (`challenger_config.json`, groups FEQ) | Source |
|---|---|
| carries/g, targets/g, rush share, target share, snap share, snap trend, opportunity trend (last <= 5 games) | `forecast_features.py` |
| red-zone opportunities/g, inside-5 carries/g (counts, not shares) | `nfl_pbp_player_week` |
| injury-exit / role-drop / blowout game classes, empirical-Bayes established workload (`xfpl_eb`), previous-season prior | `challenger.py` |
| team points/g, opponent points allowed, opponent EPA | `forecast_features.py` |

Earlier RB work this study builds on (all on the same store and scoring fingerprint `e165b59686110c7d`):

| Study | Result relevant here |
|---|---|
| `rb_projection_diagnostics.md` | Ridge loses to the season average only on low-volume backs (mean-vs-median effect); with volume it wins by 0.2-0.6 points. Receiving backs and early-down backs were both segments where ridge already beats the season average. No RB candidate was a clear improvement. |
| `opportunity_efficiency.md` | Opportunity is predictable (R^2 ~0.6), efficiency is not (R^2 <= 0.007); a position-level efficiency constant is as good as any model; a player's own efficiency history makes forecasts worse. Two ridge targets with the same features equal the direct ridge. |
| `adaptive_workload.md` | Exposure-weighted workload: no gain over ifp-2's healthy-game `xfpl_eb`. |
| `injury_workload_handoff.md` | The handoff challenger improved teammates of an absent lead (RB lead out: bias +1.07 -> +0.44) but **failed its returning-player check** (n = 185, MSE +0.04, bias +0.34 -> +0.50). This study reports the same cohorts with the same definitions (iwh-0.1.1 columns). |

What is new here: (1) **shares of team volume by situation** (inside 5 / inside 10, 1st-2nd down vs 3rd-4th down,
passing downs, two-minute, leading vs trailing by 7+), (2) **opportunity per snap**, (3) an **expected game script**
for the target week and its interaction with the player's early-down / passing-down role, (4) two **success rates kept
apart**: conventional down-and-distance success (Savant / Football Outsiders style) and nflverse EPA-positive
`success`, (5) an RB **component model** (carries, inside-5 carries and targets forecast separately, then priced and
summed).

Not repeated: touchdown regression, healthy-game windows, exposure weighting, Huber loss, boosted trees, the
opportunity/efficiency two-target model, teammate-out features.

## 2. Data, definitions and protocol

* **Inputs.** nflverse history 2019-2025 (`data/nfl_history.duckdb`) and the live 2026 database, both read-only;
  the play-by-play exits of `weekly_learning.py sync-exits`; nflverse play-by-play 2019-2026 (the release
  `app.nfl.sync` ingests) reduced to the columns in `situational_usage.PBP_COLUMNS` by the script's `pbp` command.
  Store: `forecast_backtest.build_store` over the 112 fully final weeks 2020 w1 - 2026 w5 (42,745 appearances,
  scoring fingerprint `e165b59686110c7d`), the same builder and weeks as every earlier ifp-2 study; plus the su_*
  features, the component labels and the injury-handoff cohort columns (`injury_workload` iwh-0.1.1).
* **Play classes.** `app.nfl.sync.classify_plays` (regular season only): a *designed carry* is a run that is not a
  kneel, a scramble or a two-point try; a *target* is a pass attempt with a receiver that is not a sack or a
  two-point try. Situations (fixed, `classify_situations`): early down = 1st/2nd down outside the last two minutes of
  a half; late down = 3rd/4th; passing down = 2nd and 8+ or 3rd/4th and 5+; two-minute = last 120 seconds of a half;
  leading / trailing = posteam ahead / behind by 7+ at the snap.
* **Success rates (separate names, never merged).** `su_conv_success`: a gain of at least 40% of the yards to go on
  1st down, 60% on 2nd, 100% on 3rd/4th, or a first down / touchdown. `su_epa_success`: nflverse `success`
  (EPA > 0), what the rest of the repository calls "success". Both over the player's carries + targets, shrunk toward
  the position rate by 10 opportunities.
* **Point-in-time features** (`situational_features`; `FEATURE_DOCS` lists all 19). Window: the player's last <= 5
  games played before (S, W), seasons S-1 and S, the same window as ifp-2. A split share is (player count + 5 x
  overall share) / (team count + 5): it departs from the player's overall carry / target share only with evidence.
  A window game whose team has no play-by-play is left out; a game where the player had no situational play is a
  measured zero. Opportunity per snap = (carries + targets) / (snap share x team plays). Expected script
  `su_exp_margin` = half of (team points margin per game - opponent's) over this season's final games before W, each
  shrunk toward 0 by 3 games (0 in week 1). No betting line is used: nflverse stores closing lines, which post-date a
  midweek cutoff. Interactions: `su_script_car` = expected margin x early-down carry share, `su_script_tgt` =
  expected margin x passing-down target share. Tilts: carry share when leading minus when trailing; target share
  when trailing minus when leading.
* **Component labels** (target game, evaluation and training only): carries (box score), designed carries inside the
  5, targets.
* **Models compared** (all walk-forward: every test week is predicted by fits on strictly earlier weeks, training from
  2020, asserted in code and tested): A1 recent average, A2 season average, **ifp2** = the frozen ifp-2.0.0 position
  spec refitted on the same training rows (frozen penalty and 36-week recency half-life), and the candidates below,
  which use ifp-2's frozen penalty and half-life for every fit. Conditional on appearing, identical rows for every
  comparison. ESPN: no completed player-week in the store has a pregame ESPN snapshot (the only pregame snapshot
  covers 2026 week 6+, and week 6 is not final), so no ESPN comparison is possible; ESPN is never an input.
* **Windows**: development 2021-2022 (all choices); confirmation 2023-2024, run once; exploratory 2025 + 2026 weeks
  1-5, reported only. The confirmation seasons were inspected for the *reference* models by earlier studies
  (ifp-1's selection, ifp-2's confirmation); none of the candidates below existed or was fitted on them.

## 3. Development-window diagnostics (feature distributions only, no outcomes)

`situational_usage_research.py describe`, RB rows 2021-22 with a recent game. Correlation is with the player's
overall share (rush share for RBs, target share for WR/TE).

| RB feature | p10 | p50 | p90 | corr with rush share |
|---|---|---|---|---|
| inside-5 carry share | 0.00 | 0.17 | 0.66 | 0.95 |
| inside-10 opportunity share | 0.00 | 0.10 | 0.39 | 0.89 |
| early-down carry share | 0.00 | 0.21 | 0.66 | **0.99** |
| 3rd/4th-down carry share | 0.00 | 0.19 | 0.54 | 0.89 |
| passing-down target share | 0.00 | 0.045 | 0.137 | 0.50 |
| two-minute opportunity share | 0.00 | 0.09 | 0.27 | 0.70 |
| opportunities per snap | 0.08 | 0.40 | 0.57 | 0.58 |
| carry tilt (leading - trailing) | -0.17 | 0.00 | +0.16 | -0.05 |
| expected margin | -3.9 | 0.0 | +3.8 | 0.01 |
| conventional success | 0.40 | 0.49 | 0.58 | 0.07 |
| EPA success | 0.32 | 0.41 | 0.50 | 0.03 |

Readings before any model is fitted: the RB *carry* splits are almost the same variable as the total carry share
(0.89-0.99), so little independent signal can be expected from them; the passing-down and two-minute target shares,
opportunity per snap, the tilts, the script and the two success rates are the parts that are not a restatement of
volume. For WR/TE every target split correlates 0.80-0.97 with target share. The two success rates differ by about
8 points on average for RBs (conventional 0.49 vs EPA 0.41), confirming they are different measures.

Archetype thresholds were read from these distributions (no outcome was looked at): passing-down back = >= 8% of the
team's passing-down targets (about the RB 70th percentile) and < 40% of its early-down carries; early-down back =
>= 40% of early-down carries and < 8% of passing-down targets; goal-line back = >= 50% of inside-5 carries; positive
/ negative script = expected margin >= +1.5 / <= -1.5. Development sizes: early-down backs 554 (positive script
184), passing-down backs 456 (negative script 127), goal-line backs 793, RB lead out (teammates) 483, returning
player himself 338 (all positions).

<!-- PREREG-START -->
## 4. Preregistration (committed before any candidate was fitted)

**Candidates (three, one hypothesis each).** All are fitted walk-forward per position with ifp-2's frozen penalty and
recency half-life; no hyperparameter is searched. A candidate abstains exactly where ifp2 abstains.

* **`split_add`: situational splits carry information beyond total volume.** ifp-2's position spec plus the full
  situational group: RB 16 features (inside-5 carry share, inside-10 opportunity share, early-down and 3rd/4th-down
  carry shares, passing-down and 3rd/4th-down target shares, two-minute opportunity share, target mix, opportunities
  per snap, carry and target tilts, expected margin, the two script interactions, conventional success, EPA success);
  WR/TE 11 features (inside-10, early-down, 3rd/4th-down, passing-down and two-minute target shares, opportunities
  per snap, target tilt, expected margin, script x passing-down share, the two success rates). QB unchanged.
* **`script_only`: game-script dependence is the missing piece.** ifp-2's spec plus only the script group: RB expected
  margin, script x early-down carry share, script x passing-down target share, carry tilt, target tilt; WR/TE
  expected margin, script x passing-down target share, target tilt.
* **`rb_components`: project the RB workload components and recombine.** Stage 1: three ridges (ifp-2's RB features +
  the `split_add` RB group, ifp-2's RB penalty and half-life) for next-game carries, inside-5 designed carries and
  targets. Stage 2: prices from realized components on the same training rows, label ~ b0 + b1 (carries - inside-5
  carries) + b2 inside-5 carries + b3 targets (recency-weighted least squares). Forecast = b0 + b1 (car^ - gl^) + b2 gl^
  + b3 tgt^ with components floored at 0, gl^ capped at car^, floored at 0. No player-specific efficiency term
  (`opportunity_efficiency.md` showed none helps). RB only.

**Workload question (secondary, decides nothing).** For every RB row with labels: out-of-sample MSE and MAE of
next-game carries, inside-5 carries and targets, situational stage 1 vs a volume-only twin (same ridge, ifp-2's RB
features only), week-clustered 95% CIs, development and confirmation, plus the same split by archetype and
availability cohort. WR/TE: the same for next-game targets.

**Development adoption (2021-2022 only).** For each (candidate, position) with a feature group, versus ifp2 on
identical rows: adopt if (a) MAE diff < 0 with week-clustered 95% CI upper bound < 0, (b) MSE diff <= 0, (c) |bias|
not worse by > 0.15. The adoption is written once (`frozen_adoption_ifp-su-0.1.0.json`), bound to the hash of this
block, the candidate code and the store fingerprint.

**Confirmation (2023-2024, run once).** Per adopted (candidate, position) vs ifp2 on identical rows, Bonferroni over
the number m of adopted pairs (alpha = 0.05 / m). `supported_for_shadow_tracking` only if ALL hold, otherwise
`not_supported`:

1. MAE diff <= -0.010 and the adjusted CI upper bound < 0;
2. MAE diff < 0 in 2023 and in 2024 separately;
3. MSE diff <= 0;
4. |bias| not worse by more than 0.15;
5. starters-pool MAE (top 48 RB / 60 WR / 24 TE by A1) not worse by more than 0.5%;
6. Start/Sit pair accuracy not lower by more than 0.5 percentage points;
7. guards: in every guard cohort with n >= 100 for that position (RB lead out -> teammates, RB backup takeover,
   returning player himself, RB lead returning, high volume (>= 15 opportunities/g), low volume (< 4), starters
   pool) the MAE diff's adjusted CI lower bound is <= 0 and |bias| does not grow by more than 0.15;
8. mechanism: every cohort the candidate's hypothesis names, with n >= 100, has MSE diff < 0 (`split_add`:
   early-down backs in a positive script, passing-down backs in a negative script, goal-line backs; `script_only`:
   the first two; `rb_components`: goal-line, passing-down and early-down backs).

Nothing adopted -> `not_supported`. A pass means "worth recording frozen pregame forecasts"; it is never a
promotion, which needs the existing `PROMOTION_CRITERIA` (3% over the better trailing baseline), prospective weeks
and explicit owner approval. The 2025-2026 replay and every cohort table are reported but decide nothing. Negative
results are reported plainly.

**Cohorts reported in full** (definitions in `situational_usage.cohorts()`; the handoff cohorts reuse
`injury_workload` iwh-0.1.1 columns so the injury study's failed returning-player check is re-asked): RB early-down
backs (all / positive / negative script), passing-down backs (all / negative / positive script), goal-line backs,
RB lead out -> teammates (backup workload), backup takeover (expected carry share >= 50% with the lead out), backup
after the lead returns, lead RB returning, the returning player himself, WR1 out, TE1 out, WR/TE passing-down roles
in a negative script, high / low volume, starters pool.

**Disclosures.** Before this block was written the store was built and the feature distributions and cohort sizes
above were computed on 2021-22 (no outcome, no model). The thresholds of the archetype cohorts were set from them.
No candidate has been fitted on any window.
<!-- PREREG-END -->

## 5. Development adoption (2021-2022)

`situational_usage_research.py develop`: 13,372 common-support rows in 36 weeks (RB 3,557). The refitted ifp2
reproduces the published numbers exactly (RB 4.296 / RMSE 6.021 / bias -0.12, as in `rb_projection_diagnostics.md`;
all positions 4.202, as in `opportunity_efficiency.md`), so the comparison is against the real ifp-2 spec.

| Candidate | Position | MAE diff vs ifp2 [95% CI] | MSE diff | Adopted |
|---|---|---|---|---|
| `split_add` | RB | +0.004 [-0.015, +0.024] | +0.02 | no (a, b) |
| `split_add` | WR | -0.001 [-0.009, +0.006] | +0.04 | no (a, b) |
| `split_add` | TE | +0.004 [-0.011, +0.018] | +0.07 | no (a, b) |
| `script_only` | RB | +0.003 [-0.007, +0.014] | +0.08 | no (a, b) |
| `script_only` | WR | +0.002 [-0.005, +0.008] | +0.04 | no (a, b) |
| `script_only` | TE | -0.002 [-0.009, +0.005] | -0.02 | no (a) |
| `rb_components` | RB | **-0.017 [-0.042, +0.007]** | -0.26 [-0.57, +0.07] | no (a: CI includes 0) |

**Nothing was adopted, so by the pre-registered rule every candidate is `not_supported`** and the confirmation
verdict is fixed before it ran. The confirmation and exploratory windows were still run once (write-once report) for
the descriptive tables and the cohorts the brief asks for; none of the numbers below can change a verdict.

## 6. Results on the confirmation (2023-24) and exploratory (2025-26w5) windows (descriptive)

**Points, identical rows (MAE / RMSE / bias), candidate - ifp2 MAE [95% week-clustered CI]:**

| Window, position | n | A2 season avg | ifp2 | `split_add` | `script_only` | `rb_components` |
|---|---|---|---|---|---|---|
| Conf. RB | 3,398 | 4.073 / 5.94 / +0.08 | **4.006** / 5.68 / -0.02 | 4.004 (-0.003 [-0.022, +0.016]) | 3.999 (-0.007 [-0.015, +0.001]) | 4.014 (+0.008 [-0.018, +0.033]); **MSE +0.40 [+0.14, +0.68]** |
| Conf. WR | 5,409 | 4.288 | 4.211 | 4.209 (-0.003 [-0.009, +0.004]) | 4.212 (+0.001) | = ifp2 |
| Conf. TE | 3,208 | 2.892 | 2.853 | 2.857 (+0.005 [-0.005, +0.014]) | 2.853 (+0.000) | = ifp2 |
| Expl. RB | 2,196 | 4.074 | 4.067 | 4.074 (+0.007 [-0.012, +0.025]) | 4.064 (-0.003 [-0.015, +0.009]) | 4.084 (+0.017 [-0.004, +0.038]) |
| Expl. WR | 3,477 | 4.285 | 4.061 | 4.061 (+0.001) | 4.060 (-0.000) | = ifp2 |
| Expl. TE | 2,138 | 3.131 | 3.039 | 3.041 (+0.002) | 3.036 (-0.003) | = ifp2 |

Start/Sit pairs (all positions, confirmation): ifp2 65.57% / regret 2.495; `split_add` 65.46% / 2.507;
`script_only` 65.53% / 2.498; `rb_components` 65.45% / 2.514. Every candidate is within noise of ifp2 or worse.

**Workload: do splits predict next-week workload better than total volume?** Situational stage 1 (ifp-2 features +
the `split_add` group) minus the volume-only stage 1 (ifp-2 features), MSE diff [95% CI]:

| Component | Development | Confirmation | Exploratory |
|---|---|---|---|
| RB carries (MSE ~19-20) | -0.120 [-0.276, +0.018] | +0.056 [-0.070, +0.171] | -0.104 [-0.200, +0.005] |
| RB inside-5 carries (MSE ~0.5) | +0.003 [+0.000, +0.005] | -0.001 [-0.003, +0.001] | -0.001 [-0.005, +0.003] |
| RB targets (MSE ~2.7-3.3) | -0.005 [-0.029, +0.021] | +0.002 [-0.016, +0.021] | -0.003 [-0.019, +0.013] |
| WR targets | +0.011 [-0.007, +0.028] | +0.002 [-0.010, +0.014] | -0.007 [-0.024, +0.010] |
| TE targets | +0.004 [-0.013, +0.022] | +0.015 [-0.002, +0.030] | -0.004 [-0.022, +0.013] |

No component improves consistently; the largest effects are well under 1% of the error and change sign across
windows. Inside-5 carries, the "goal-line share" Savant-style metric, are forecast no better with the inside-5 share
than with ifp-2's per-game inside-5 count. **Answer to the question: no — situational splits do not predict next-week
workload better than total volume.** By archetype the same holds (confirmation carry MSE diff: early-down backs +0.23
[-0.37, +0.74], passing-down backs +0.28 [-0.07, +0.69], goal-line backs +0.13 [-0.28, +0.50]).

**RB archetype and availability cohorts (confirmation; MAE, bias in parentheses; [CI] = candidate - ifp2 MAE):**

| Cohort | n | ifp2 | `split_add` | `script_only` | `rb_components` | A2 |
|---|---|---|---|---|---|---|
| Early-down backs | 481 | 5.579 (+0.21) | 5.606 [-0.011, +0.064] | 5.592 [-0.006, +0.032] | 5.694 [+0.054, +0.166] | 5.971 |
| ... in a positive script | 156 | 5.577 (+0.56) | 5.653 [+0.012, +0.134] | 5.616 [+0.008, +0.073] | 5.680 [+0.019, +0.194] | 6.050 |
| ... in a negative script | 137 | 5.508 (+0.04) | 5.477 [-0.114, +0.054] | 5.503 | 5.675 [+0.024, +0.301] | 5.766 |
| Passing-down backs | 380 | 4.572 (+0.30) | 4.623 [+0.002, +0.107] | 4.579 | 4.531 [-0.090, +0.003] | 4.744 |
| ... in a negative script | 125 | 4.476 (-0.10) | 4.507 [-0.068, +0.121] | 4.419 [-0.128, +0.016] | 4.599 [+0.027, +0.221] | 4.699 |
| Goal-line backs | 804 | 5.858 (-0.13) | 5.864 [-0.036, +0.044] | 5.848 [-0.038, +0.019] | 5.954 [+0.051, +0.143] | 6.259 |
| **RB lead out -> teammates (backup workload)** | 357 | 3.947 (**+1.07**) | 3.961 (+1.09) | 3.951 (+1.10) | 3.898 (+1.03) [-0.107, +0.011] | 4.063 |
| RB backup takeover (expected share >= 50%) | 43 | 6.524 (**+3.90**) | 6.626 (+3.93) | 6.521 (+3.94) | 6.504 (+3.54) | 8.084 |
| RB backup after the lead returns | 46 | 3.271 (-1.09) | 3.133 (-1.09) | 3.199 | 3.305 | 3.120 |
| **Lead RB returning** | 18 | 4.802 (-1.30) | 4.811 (-1.05) | 4.788 | 5.035 | 5.252 |
| **Returning player himself (RB)** | 78 | 3.886 (+0.59) | 3.842 (+0.69) [-0.137, +0.030]; MSE +0.41 | 3.846 [-0.096, +0.012]; MSE -0.40 | 3.978 [-0.008, +0.192]; MSE +3.02 | 3.897 |
| High volume (>= 15 opp/g) | 867 | 6.223 | 6.215 | 6.201 | 6.320 [+0.040, +0.151] | 6.475 |
| Low volume (< 4 opp/g) | 1,184 | 2.043 (-0.05) | 2.008 [-0.056, -0.014] | 2.035 | 1.947 (+0.13) [-0.129, -0.062] | 1.893 |
| Starters pool (top 48) | 1,728 | 5.437 | 5.456 | 5.426 | 5.490 [+0.014, +0.090] | 5.754 |

WR/TE (confirmation, `split_add`): WR1 out -> teammates (n 561) 3.807 -> 3.781 [-0.051, -0.003], bias +0.67 unchanged
in substance; TE1 out (340) 2.465 -> 2.452 [-0.038, +0.012]; WR/TE in passing-down roles with an expected deficit
(355 / 83) -0.014 [-0.058, +0.025] / +0.112 [-0.032, +0.259]; returning WR (119) -0.024 [-0.067, +0.023], returning
TE (59) -0.011. The exploratory replay shows the same pattern (WR1 out -0.010 [-0.020, -0.002], returning TE (47)
**+0.078 [+0.024, +0.142]**).

Readings:

* **The game-script hypothesis is not supported.** The cohort it targets most directly, early-down backs with an
  expected lead, gets *worse* under both script candidates in the confirmation window (+0.076 and +0.038, CIs above
  0) and in the exploratory replay (+0.049, +0.054). ifp2 already under-projects that cohort slightly (bias +0.56),
  but the script terms do not fix it. A season-to-date point margin is a weak pregame proxy for script; without a
  pregame betting line (not in our data at a midweek cutoff) there is no better one.
* **The passing-down / early-down distinction is real in the data but already priced in.** Both archetypes are
  forecast well by ifp2 (it beats the season average by 0.17-0.39 in each), and their workload is forecast equally well
  from total volume.
* **`rb_components` failed for an understood reason.** Its development gain was concentrated in low-volume backs
  (-0.074, bias -0.18 -> -0.04) and it is worse on every volume cohort in both later windows (starters pool +0.053,
  goal-line backs +0.096, early-down backs +0.115, MSE +0.40 pooled). Pricing every carry and target at a
  position-wide rate removes player-specific value per opportunity that the direct ridge keeps through its production
  features; the low-volume improvement is the mean-versus-median MAE effect documented in
  `rb_projection_diagnostics.md`, not a better mean. This refines `opportunity_efficiency.md`: a position-level
  efficiency constant is fine for the *residual*, but replacing the direct model by priced components is not.
* **Backup workload / RB1 out: situational splits do not touch the real problem.** With the lead back out, ifp2's
  teammates are under-projected by +1.07 points (backup takeover +3.90), and no candidate moves that (+1.03 to +1.10).
  A back's past situational shares describe the room he played in; they cannot know the room changed. That bias is a
  teammate-availability problem, the subject of `injury_workload_handoff.md` (which cut it to +0.44).
* **Returning starters: nothing here passes or fails the earlier check, because nothing is adopted.** The cohort is
  small for RB (78 rows; lead RB returning 18). `split_add` lowers its MAE by 0.045 but raises MSE by 0.41 and bias
  from +0.59 to +0.69, the same "mean fixed in one direction, error wider" pattern the handoff study failed on;
  `rb_components` is worse (MSE +3.0). The returning-player question stays open and needs prospective rows.
* **Conventional vs EPA success.** Both were in the `split_add` group under separate names; neither added
  measurable accuracy (the group as a whole is null). They remain different measures (RB means 0.49 vs 0.41).

## 7. Verdict

**`not_supported` for every candidate** (pre-registered rule: nothing adopted on 2021-22; the later windows confirm
the null and show `rb_components` is significantly worse on squared error). Plainly:

1. Situational usage splits (goal-line / inside-10 shares, down-and-distance splits, two-minute usage,
   leading/trailing usage, opportunity per snap) **do not predict workload or points better than the total-volume
   features ifp-2 already has**, for RB, WR or TE. For RBs the carry splits are almost the same variable as total
   carry share (correlation 0.89-0.99); the parts that are not (passing-down and two-minute target share, tilts,
   success rates) carry no forecastable signal at a one-week horizon.
2. **Projecting RB components separately and recombining is worse than ifp-2's direct ridge** once evaluated outside
   the development window (it trades player-specific value per touch for a lower MAE on low-volume backs).
3. **Game-script conditioning does not help**, including for early-down backs who depend on a positive script; the
   pregame script proxy available without betting lines is too weak.
4. The two cohorts the brief singles out are reported: the RB1-out backup workload bias (+1.07) is untouched by
   situational features (it needs teammate availability, as in the handoff study), and the returning-starter cohort is
   too small to adjudicate historically.

Nothing is promoted; ifp-1.0.0, ifp-2.0.0, the registry, the weekly pipeline, the ledgers and the "Ours" selector
are unchanged. No prospective shadow is recommended.

**Disposition (2026-10-18, owner direction: Savant signals should improve Ours, not become a separate model).**
Every candidate here was already framed that way: ifp-2's own position spec plus situational features (or
ifp-2's features feeding RB component forecasts), fitted on the same training rows and scored out of sample against
ifp-2, with the availability cohorts taken from the iwh RB workload pieces on main (#111). None gained, so **the
situational features are dropped**: they are not added to the Ours (ifp) feature set, no standalone model or shadow
ledger is built, and the research module stays unused by the app.

**Recommendation for the NFL Savant review:** do not build situational usage splits into the projection engine.
If Savant-style splits are wanted, they belong in **descriptive** player pages (role profile: early-down vs
passing-down back, goal-line share, conventional success rate labelled as such), not in the forecast. Do not repeat:
situational shares as ridge features, component recombination at position-wide prices, season-margin game script.

## 8. Limitations, reproduction, what changed

* **No fresh holdout.** 2023-2026w5 had been inspected for the reference models; the candidates were frozen on
  2021-22 first, and since none was adopted, the later windows are descriptive only. Prospective frozen forecasts from
  2026 week 6 are the only clean evidence; none was recorded because nothing passed.
* **ESPN not compared**: no completed player-week in the store has a pregame ESPN snapshot (the only one covers 2026
  week 6+, not final when this ran).
* **No route participation or pregame lines.** nflverse participation data was not used (snaps come from snap
  counts); opportunity per snap is (carries + targets) / (snap share x team plays), an approximation. Game script uses
  season-to-date margins only.
* **Fixed situation definitions** (lead margin 7, passing-down thresholds, two-minute = 120 s, shrinkage pseudo-counts
  5 and 10, archetype thresholds from development distributions); none was tuned on outcomes.
* Conditional on appearing, as ifp-2; the handoff cohort columns are iwh-0.1.1 (the version with the
  Questionable-inactive fix), so the returning-player cohort is not identical to the injury study's 0.1.0 cohort.
* MAE is the headline (median-friendly); squared error and bias are reported throughout because `expected_points`
  needs a mean.

What changed in the repository: the research module, its script, its tests, this report, and the two isolation-test
allow-lists (which name the new module). Nothing in `app/` imports it (tested).

Reproduce (about 3 minutes; play-by-play download is cached by nflreadpy; outputs git-ignored under
`<ifp-dir>/research/situational_usage/`; `develop` and `confirm` are write-once, so use a fresh `--ifp-dir`):

```
cd backend
uv run python ../scripts/situational_usage_research.py pbp
uv run python ../scripts/situational_usage_research.py build
uv run python ../scripts/situational_usage_research.py describe
uv run python ../scripts/situational_usage_research.py develop
uv run python ../scripts/situational_usage_research.py confirm
```

Run on 2026-10-18 with nflverse as published that day; store fingerprint `5f55c24eb112f66a`, play-by-play hash
`c481eac0516f54bb`, preregistration committed in `2a0a849` before `develop`.

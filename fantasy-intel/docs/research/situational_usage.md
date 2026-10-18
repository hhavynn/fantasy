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

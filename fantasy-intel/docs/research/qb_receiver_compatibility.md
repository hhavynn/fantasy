# QB-receiver compatibility: receiver role x expected-QB passing quality (`ifp-qbr-0.1.1`, research only)

**Question.** ifp-ctx (`qb_teammate_context.md`) found that a changed expected starting QB helps WR projections
only a little: the changed-QB WR cohort improves by 0.03-0.10 MAE, and the QB group (candidate A) is flat for WR/TE
pooled. A's QB deltas are role-blind: every receiver on a team gets the same EPA/CPOE delta. The hypothesis tested
here: **a deep-threat receiver (high deep-target share, high air-yards share) loses more expected production with a
weaker or shorter-throwing backup QB than a short-route receiver on the same team**, so role x QB-delta interactions
should capture more of the QB change than A does.

**Short answer: no.**

* **Not adopted, verdict `retain_baseline`.** Neither WR nor TE meets the pre-registered development rule.
  * WR: Q minus ifp-2 is -0.005 [-0.018, +0.007]; Q minus A is -0.007 [-0.014, +0.000]. The rule needs at most
    -0.010 with CI upper < 0 against ifp-2, *and* CI upper < 0 against A.
  * TE: +0.000 against ifp-2, -0.000 against A.
* **Role-awareness adds about nothing beyond A in any window.** Pooled WR+TE, Q minus A is -0.004 (dev), -0.001
  (confirmation) and -0.004 (replay). Adding the interactions to ifp-ctx-p1's WR spec (C -> CQ)
  moves WR by -0.005 / -0.002 / -0.003, also within noise. The development CI for pooled Q minus A just touches 0
  ([-0.009, -0.000]), but selection is per position, and neither WR nor TE passes.
* **On the cohort the interactions exist for, they are flat.** On changed-expected-QB rows, Q minus A is
  -0.004 [-0.023, +0.014] (dev), -0.002 [-0.016, +0.011] (confirmation) and +0.004 [-0.013, +0.021] (replay).
  A itself keeps its -0.04 to -0.06 gain there. The role split of that cohort is not consistent either: deep threats
  -0.035 / +0.009 / +0.023, short-route receivers -0.001 / +0.000 / +0.001.
* **The hypothesis' direct prediction fails** (section 5). When the expected QB is a downgrade, ifp-2 does *not*
  over-project deep-threat WRs more than short-route WRs. Short-route WRs are over-projected more in development and
  confirmation (-0.94 vs -0.67; -1.15 vs -0.22). Compared on the same team-week, the deep-minus-short residual
  has no consistent sign, and every CI covers 0.
* **Why so little signal.** Deep-ball accuracy is barely a QB trait. Within-QB play noise equals the between-QB
  spread of deep CPOE only at about 260 deep attempts, roughly two full seasons. After shrinkage, the deep-CPOE delta
  of a changed QB has an interquartile range of only -1.5 to +0.9 points. What role signal exists runs through the
  air-yards share x EPA delta term, which overlaps A's own EPA delta.
* **The bias guard passes**, unlike the QB-row adoption it sank in ifp-ctx. WR/TE |bias| moves by less than 0.01 in
  every window. QB rows are out of scope here by pre-registration.
* **Dropped as an Ours feature.** It was tested as a candidate addition to the Ours feature set, out of sample
  against ifp-2. It does not beat ifp-2 for WR or TE in any window, so nothing is added to Ours (ifp-2 / ifp-2.1),
  and served numbers are unchanged. No new prospective candidate is proposed. `ifp-ctx-p1` and its write-once ledger
  are untouched (tested). The research module stays only so the negative result can be reproduced.

Code: `backend/app/analytics/qb_receiver_compat.py` (play-by-play tables, QB profiles, receiver roles, interactions,
candidates, walk-forward, cohorts, pre-registered rules), `scripts/qbr_research.py` (`pbp`, `build`, `develop`,
`evaluate`, `hypothesis`), tests in `backend/tests/test_qb_receiver_compat.py`. Outputs go to
`data/models/research/qbr/` (git-ignored), with the ifp-ctx store cached in its own `work/` subdirectory. Nothing in
the app imports the module (tested). ifp-1.0.0, ifp-2.0.0, the registry, production, ifp-ctx and ifp-ctx-p1 are
unchanged.

## 1. Data

**Source.** nflverse play-by-play (`nflreadpy.load_pbp`). It is the same free CC-BY feed `sync.py` already derives
`nfl_pbp_player_week` from, and it is not a new data dependency. NFL Savant is not used: everything it shows for
passing is derivable from these plays.

* `qbr_research.py pbp` caches the REG-season pass plays of 2018-2026, about 20,000 per season, in
  `research/qbr/pbp/`. It keeps only the columns in `qr.PBP_COLUMNS`: `air_yards`, `cpoe`, `epa`, `qb_epa`, the
  passer/receiver/rusher ids, and the dropback, sack, scramble and two-point flags.
* Only per-play fields the live feed carries are used (the coverage audit lists them as safe weekly inputs).
* NGS passing is not used, so this study does not depend on the sibling NGS ingestion.
* There is no true pressure flag in play-by-play, so pressure is represented by **sack rate**.

**Aggregation (`pass_game_tables`).** Two-point tries are excluded.

* QB-game rows:
  * dropbacks include sacks and scrambles (a scramble is charged to the scrambling QB);
  * attempts exclude sacks;
  * CPOE, air-yards and deep (air_yards >= 20) sums count only plays that have a value.
* Receiver-game rows: targets, air yards, deep targets and EPA, joined to the team's targets and air yards in that
  game.

## 2. Definitions (fixed before the first points backtest; `qb_receiver_compat.py`)

**QB passing profile, as of week W.** It covers the current season plus 2 previous seasons, using plays of games
before W only. It holds CPOE, deep-ball CPOE, deep-attempt rate, aDOT, sack rate and EPA per dropback.

* Each rate is shrunk toward the **replacement-level prior**: pooled rates of completed QB-seasons with fewer than
  200 dropbacks. This is ifp-ctx's rule, so a backup with 15 attempts is pulled toward backups, not toward starters.
* A known QB with no attempts gets the prior. An unknown QB gets null.
* The shrinkage constants are the samples at which within-QB play variance equals the between-QB variance of
  QB-seasons (reliability 0.5). They were measured once by method of moments on 2018-2020 plays (no points labels,
  before the development window), then rounded and frozen:

| Rate | Measured (2018-20) | Frozen | Note |
|---|---|---|---|
| QB CPOE | 237 attempts | 250 | ifp-ctx uses 200 |
| QB deep CPOE | 264 deep attempts | 250 | between-QB SD 2.9 pts vs play SD 47.5: barely a QB trait |
| QB deep-attempt rate | 242 attempts | 250 | |
| QB aDOT | 147 attempts | 150 | |
| QB sack rate | 198 dropbacks | 200 | |
| QB EPA/dropback | 205 dropbacks | 200 | ifp-ctx uses 200 |
| Receiver aDOT | 4.7 targets | 5 | a stable trait |
| Receiver deep-target share | 17 targets | 20 | |
| Receiver EPA/target | 121 targets | 120 | |

**Expected starter and recent QBs.** These are ifp-ctx's, unchanged.

* The expected starter is the `expected_qb_id` of the ctx store, from `roster_context.expected_starters` (95.8%
  correct when `supported`).
* The recent QBs are the starters of the player's last <= 5 games.
* Every delta is the expected starter's profile minus the mean profile of the recent QBs, both as of W. So the same
  QB in every recent game gives a delta of exactly 0 (tested).

**Receiver role.** It covers the player's last <= 10 games with a target before W (current and previous season,
any team).

* aDOT, deep-target share and EPA per target are each shrunk toward his position's league rate.
* Air-yards share and target share are of his team's totals in those games, unshrunk.

**Interactions** (WR and TE rows only):

* `qbr_deep_x` = deep-target share x deep-CPOE delta
* `qbr_vol_x` = deep-target share x deep-attempt-rate delta
* `qbr_epa_x` = air-yards share x ifp-ctx's `qb_epa_delta`

A's main effects (`qb_epa_delta`, `qb_cpoe_delta`, `qb_changed`) stay in the model. So the QB effect becomes
b1 x delta + b2 x role x delta, a slope that depends on the receiver's role, which is the hypothesis. Unknown stays
null: no expected QB, no recent starter or no target history gives null, and the ridge imputes the training mean
with a `__missing` indicator.

**Label-free diagnostics** (`build`, WR+TE rows 2020-2026, no points labels):

* Features are null for 10-12% of rows per season, and 28% of 2026 weeks 1-6.
* The play-by-play CPOE delta correlates 0.98 with ifp-ctx's `nfl_weekly_stats` CPOE delta.
* WR deep-target share quartiles are 0.13 / 0.16 / 0.20. TE median is 0.06.
* On changed-QB rows (10th to 90th percentile):
  * deep-CPOE delta: -2.6 to +1.9;
  * deep-rate delta: -0.022 to +0.022;
  * EPA delta: -0.17 to +0.11.
* The deep-CPOE delta correlates 0.60 with the EPA delta.

**One disclosed change before any points backtest.** The short-route threshold was first written as 0.10 and
labelled "about the bottom quarter of WR rows". The build showed 0.10 is the 10th percentile. It was moved to 0.13
(the WR 25th percentile) and committed before the first development run. Deep threat is >= 0.20 (the WR 75th
percentile). A QB "downgrade" is a changed expected QB with an EPA delta below -0.03. These thresholds define
reported cohorts only and select nothing.

## 3. Protocol (pre-registered in `qb_receiver_compat.DECISION_RULES`, committed before the first development run)

The protocol is identical to `qb_teammate_context.md` section 4, with the same thresholds, windows, walk-forward
(`challenger.train_rows`, strictly earlier weeks, asserted) and week-clustered bootstrap (1,000 resamples).

* **Candidates** (WR and TE rows; QB and RB keep ifp-2 by pre-registration):
  * **A**: ifp-ctx's QB group (comparator);
  * **Q** = A + the interactions (the candidate);
  * **C**: ifp-ctx-p1's WR spec, QB + teammate context (comparator);
  * **CQ** = C + the interactions (descriptive).
* Every candidate keeps ifp-2's spec, frozen penalty and recency weights. No penalty or hyperparameter is searched.
* The script asserts that ifp-2, A and C equal `context_challenger.walk_forward` exactly on the same rows, and the
  tests check ifp-2 against `challenger.challenger_predictions`.
* **Selection** (development 2021-22 only, per position). Q is adopted if all of these hold:
  * MAE(Q) - MAE(ifp-2) <= -0.010, with CI upper < 0;
  * |bias| is not worse than ifp-2's by more than 0.15 (the guard that sank ifp-ctx's QB-row adoption);
  * **MAE(Q) - MAE(A) < 0 with CI upper < 0**, so a gain must come from role-awareness and not from A's role-blind
    QB context.
* **Confirmation** (2023-24, run once): c1-c6 as ifp-ctx. The mechanism check c7 requires that, on each adopted
  position's changed-expected-QB rows, MAE(qbr) < MAE(ifp-2) **and** MAE(qbr) < MAE(A).
* **Exploratory** (2025-26): reported only. It was already inspected, and its injury reports are untimestamped.

**Re-run after code review (disclosed).** The review of PR #124 found a look-ahead in `qbr-0.1.0`.
* Each receiver's position, which picks the league prior his role rates shrink toward, came from his *latest*
  panel row overall. A later re-listing (a WR later listed as TE) could therefore reach earlier backtest weeks.
* `qbr-0.1.1` takes the position from panel rows strictly before (S, W) (`positions_before`, tested). The rules
  are unchanged.
* Development, confirmation and the replay were re-run once. The selection (every position `ifp2`) and the verdict
  are identical. No headline figure moved by more than 0.002 MAE, and a few role-cohort rows changed membership
  (for example confirmation WR deep threats 259 -> 265). Every number below is from the re-run. The
  confirmation window has now been evaluated twice by this study.

## 4. Results

### Development (2021-22) and the frozen selection

Identical rows, n = 8,535 (WR 5,324, TE 3,211), 36 weeks.

| Position | ifp-2 MAE | A - ifp-2 | Q - ifp-2 | Q - A | Bias ifp-2 -> Q | C - ifp-2 | CQ - C | Adopted |
|---|---|---|---|---|---|---|---|---|
| WR | 4.438 | +0.002 | -0.005 [-0.018, +0.007] | -0.007 [-0.014, +0.000] | -0.281 -> -0.282 | -0.017 [-0.031, -0.003] | -0.005 [-0.012, +0.001] | ifp2 |
| TE | 2.990 | +0.000 | +0.000 [-0.010, +0.011] | -0.000 [-0.007, +0.008] | -0.060 -> -0.063 | +0.001 | +0.000 | ifp2 |
| WR+TE | 3.893 | +0.001 | -0.003 [-0.011, +0.005] | -0.004 [-0.009, -0.000] | -0.198 -> -0.200 | -0.010 [-0.019, -0.000] | -0.003 | |

The ifp-2, A and C numbers reproduce `qb_teammate_context.md` section 6 exactly (WR 4.438, +0.002, -0.017; TE 2.990,
+0.000, +0.001). The selection is frozen write-once in `frozen_selection_ifp-qbr-0.1.1.json`: every position `ifp2`. That matches the pre-review
`frozen_selection_ifp-qbr-0.1.0.json`, which is kept unchanged.
With nothing adopted, the outcome is `retain_baseline` by rule. The numbers below are descriptive and decide
nothing.

### All windows

conf = 2023-24 (n = 8,617, 36 weeks); expl = 2025-26 (n = 5,615, 23 weeks, retrospective injury reports).
`*` marks a CI that excludes 0.

| Rows | Comparison | dev | conf | expl |
|---|---|---|---|---|
| WR | Q - ifp-2 | -0.005 | +0.000 | -0.003 |
| WR | Q - A | -0.007 | -0.003 | -0.003 |
| WR | CQ - C | -0.005 | -0.002 | -0.003 |
| TE | Q - ifp-2 | +0.000 | +0.006 | -0.008* |
| TE | Q - A | -0.000 | +0.003 | -0.006 |
| WR+TE | Q - ifp-2 | -0.003 | +0.002 | -0.005 |
| WR+TE | Q - A | -0.004 | -0.001 | -0.004 |
| WR+TE | CQ - C | -0.003 | +0.000 | -0.004 |
| WR+TE | RMSE ifp-2 -> Q | 5.513 -> 5.510 | 5.383 -> 5.385 | 5.316 -> 5.315 |
| WR+TE | Start/Sit pair accuracy ifp-2 -> Q | 64.80% -> 64.80% | 64.19% -> 64.10% | 64.11% -> 64.12% |

By season in the replay, WR+TE Q minus A is -0.004 [-0.009, +0.001] in 2025 and -0.004 [-0.009, +0.003] in 2026
weeks 1-6. Starters pool, Q minus ifp-2: +0.000 / +0.002 / -0.007.

### The changed-expected-QB cohort, by role

Rows whose expected starter differs from the most frequent starter of the player's last five games (pregame).
MAE differences; "bias" is mean(actual - forecast).

| Rows | Cohort | n dev / conf / expl | A - ifp-2 (dev / conf / expl) | Q - A (dev / conf / expl) | Bias ifp-2 / A / Q (conf) |
|---|---|---|---|---|---|
| WR | changed QB | 1,099 / 996 / 706 | -0.048* / -0.059* / -0.029* | -0.004 / -0.013 / +0.005 | -0.04 / +0.16 / +0.18 |
| TE | changed QB | 639 / 576 / 414 | -0.058* / -0.037* / -0.059* | -0.004 / +0.016 / +0.001 | +0.08 / +0.24 / +0.24 |
| WR+TE | changed QB | 1,738 / 1,572 / 1,120 | -0.051* / -0.051* / -0.040* | -0.004 / -0.002 / +0.004 | +0.00 / +0.19 / +0.20 |
| WR | changed QB, deep threat | 231 / 265 / 177 | -0.030 / -0.043* / -0.026 | -0.037 / +0.008 / +0.023 | +0.11 / +0.32 / +0.35 |
| WR | changed QB, short route | 312 / 234 / 148 | -0.038 / -0.070* / -0.022 | +0.004 / -0.015 / +0.009 | -0.22 / -0.01 / +0.02 |
| WR | QB downgrade | 580 / 499 / 353 | -0.081* / -0.065* / -0.047* | -0.020 / -0.011 / +0.028 | -0.30 / -0.07 / -0.01 |
| WR | downgrade, deep threat | 119 / 128 / 87 | -0.097* / -0.052* / -0.040* | -0.053 / -0.002 / +0.050 | -0.22 / +0.03 / +0.27 |
| WR | downgrade, short route | 163 / 122 / 78 | -0.084* / -0.077* / -0.010 | +0.017 / -0.033 / +0.028 | -1.15 / -0.92 / -0.92 |
| WR | backup QB expected (starter out) | 181 / 154 / 142 | -0.070* / -0.020 / -0.050* | -0.050 / -0.070* / +0.078* | +0.20 / +0.44 / +0.45 |
| WR+TE | same QB | 6,359 / 6,621 / 4,055 | +0.013* / +0.012* / +0.008* | -0.005* / -0.000 / -0.007* | +0.13 / +0.09 / +0.08 |

Reading:

* **A is where the QB-change gain lives.** It improves every changed-QB cohort in every window.
* **Q adds nothing consistent on top.** The only significant Q-minus-A cells change sign between windows. With a
  backup QB expected, WR moves -0.070 in confirmation and +0.078 in the replay.
* **A's pooled flatness for receivers is a trade.** It gains about 0.05 on changed-QB rows and pays +0.008 to +0.013
  on the about 75% of rows with the same QB, every window significant. Q recovers a little of that cost in dev and
  the replay (-0.005, -0.007), not in confirmation.
* **The bias guard passes everywhere.** A and Q shift the changed-QB bias by about +0.15 to +0.2 (toward
  under-projection in confirmation), but pooled WR/TE |bias| moves by less than 0.01. QB rows, whose bias worsened
  by 0.27 in ifp-ctx, are not part of this candidate.

## 5. The hypothesis directly (`qbr_research.py hypothesis`, descriptive)

WR rows with a changed expected QB and known role and deltas: n = 1,062 / 962 / 670. Downgrades (EPA delta < -0.03):
561 / 483 / 335. A residual is actual minus forecast, so negative means over-projected. Bootstrap 95% CIs: by week
for the slopes, by team-week for the paired test.

| Test | dev | conf | expl | The hypothesis predicts |
|---|---|---|---|---|
| ifp-2 residual, downgrade, deep threat (n 119 / 128 / 87) | -0.67 | -0.22 | +0.42 | more negative than short route |
| ifp-2 residual, downgrade, middle (n 279 / 233 / 170) | -0.63 | -0.05 | -0.69 | |
| ifp-2 residual, downgrade, short route (n 163 / 122 / 78) | -0.94 | -1.15 | +0.72 | |
| Same team-week, deep threat minus short route, ifp-2 residual (team-weeks 49 / 56 / 31) | -0.86 [-2.85, +1.12] | +0.57 [-1.48, +2.58] | +0.98 [-1.55, +3.51] | < 0 |
| Slope of A's residual on (deep share - mean) x EPA delta | +15 [-31, +55] | -39 [-94, +20] | +5 [-62, +87] | > 0 |
| Slope of A's residual on (deep share - mean) x deep-CPOE delta | -1.2 [-4.8, +2.2] | +0.4 [-3.5, +4.2] | +0.7 [-2.7, +4.3] | > 0 |

* **Over-projection after a downgrade is not larger for deep threats.**
  * Short-route WRs are over-projected more in development and confirmation (-0.94 and -1.15, against -0.67 and
    -0.22).
  * In the replay both groups are under-projected.
* **On the same team-week the sign flips between windows**, and no CI excludes 0. These are only 31-56 team-weeks
  per window, so a modest effect could hide in the noise. But no window points the hypothesized way with any
  strength.
* **A's residual does not depend on role x QB delta.** Both slopes change sign across windows, with CIs far wider
  than the estimates.

**What Q fitted anyway.** Q's WR coefficients per raw unit, at the start of each window:

| Window start | `qb_epa_delta` | `qbr_epa_x` (air-yards share x EPA delta) | `qbr_deep_x` (deep share x deep-CPOE delta) | `qbr_vol_x` (deep share x deep-rate delta) |
|---|---|---|---|---|
| 2021 week 1 | -1.96 | +28.6 | -0.52 | -22 |
| 2023 week 1 | -3.94 | +26.2 | -0.13 | -92 |
| 2025 week 1 | -3.91 | +23.8 | -0.18 | -81 |

Take a 10th-percentile changed-QB downgrade: EPA delta -0.17, CPOE -4.3, deep CPOE -2.6, deep rate -0.02.

* For a deep threat (deep share 0.25, air-yards share 0.35), the QB terms give -0.62 / -0.31 / -0.46 points.
* For a short-route WR (0.10, 0.10), they give +0.33 / +0.48 / +0.24.

So the model does split receivers by role, by about 0.7-1.0 points, almost entirely through air-yards share x EPA
delta. The two deep-specific terms take the *opposite* sign to the hypothesis. Section 4 shows this split does not
reduce error out of sample, and the residual tests above show the realized outcomes do not split that way. The role
term is fitting noise that happens to have the expected shape.

## 6. Verdict and recommendation

**`retain_baseline`.** Receiver role x expected-QB interactions do not improve on ifp-ctx's role-blind QB context
for WR or TE rows, pooled or on the changed-QB cohort. The direct test finds no role dependence of the QB-change
error either. The ifp-ctx finding stands: what a QB change does to receivers is captured, as far as it can be, by A's
team-level deltas (about -0.05 MAE on changed-QB rows). That cohort is too small, and too much of the gain is paid
back on same-QB rows, to move pooled receiver MAE.

* **No prospective candidate is proposed.** Nothing here meets even the development rule, so a new pre-registered
  candidate would not have a basis. `ifp-ctx-p1` (frozen, write-once ledger from 2026 week 7) is unchanged and not
  extended.
* **What would still be worth testing**, in order:
  1. **The same-QB cost of A.** A is worse than ifp-2 by +0.008 to +0.013 on same-QB receiver rows in every window,
     and that cancels its changed-QB gain. Two causes are plausible, and neither is tested here:
     * rows whose five-game window mixes QBs carry non-zero deltas even when the expected QB is the usual starter;
     * the refit shifts the shared coefficients.

     Gating A's deltas on `qb_changed` (delta x changed) is a one-feature change that can be pre-registered. It is a
     different hypothesis from this one.
  2. **Pair-level target history** (how often this receiver was targeted by *this* QB). It needs only the cached
     play-by-play, but backup QBs have tiny pair samples, so expect the shrinkage problem of deep CPOE.
  3. **Not recommended**: more deep-ball QB measures. Deep CPOE is barely a QB trait at the sample sizes backups have.

## Reproduce

```
cd backend
uv run python ../scripts/qbr_research.py pbp          # once: caches 2018-2026 pass plays (~20k/season)
uv run python ../scripts/qbr_research.py build
uv run python ../scripts/qbr_research.py develop      # writes the frozen selection once
uv run python ../scripts/qbr_research.py evaluate     # confirmation + replay, evaluation.json
uv run python ../scripts/qbr_research.py hypothesis   # section 5, hypothesis.json
```

Paths come from `IFP_MODEL_DIR`, `WEEKLY_MODEL_DIR`, `NFL_HISTORY_DB_PATH` and `FANTASY_DB_PATH`, as for the other
research scripts. A worktree without `data/` must point them at the main checkout's `data/`. Runtime is about 5-10
minutes to build the cached ifp-ctx store (it rebuilds whenever any app or script file changes, because its
fingerprint covers all code), about 30 s for the role columns and about 5 s per walk-forward window.

# First-read targets, depth charts and usage signals (PR 9, research only)

**Question.** Do FTN read-progression charting and nflverse's timestamped depth charts predict
next-week fantasy opportunity better than the features ifp-2 already uses?

**Short answer.**

* **FTN first-read share: small but real, WR and RB only.** It continues to a prospective shadow
  test. Points MAE drops by 0.020 in development and 0.012 on the already-inspected replay, both
  with week-clustered CIs below 0. TE gains nothing. Start/Sit is unchanged. All of this depends
  on reading code `"1"`, not the dictionary's `"0"`, as the first read (see below).
* **Depth charts: the pre-registered depth-change group passes, but the ablation shows that the
  "change" part does not work.** Leaving out `dc_tier_change`, `dc_vacancy` or `dc_snap_gap`
  *lowers* MAE. All of the value comes from the static pregame tier (`dc_tier`), and 53% of it
  comes from 161 backup-QB appearances. The depth-change hypothesis is **rejected**. The static
  tier goes forward as a new, separately pre-registered candidate, with only 1.3 seasons of
  evidence and no untouched holdout.
* **Combined (C): adds nothing over depth alone** on RB/WR/TE.
* Nothing is integrated. ifp-1.0.0 and ifp-2.0.0, the ledgers, the ESPN/Ours switch, Start/Sit,
  trades, waivers and the API are untouched.

Code: `backend/app/analytics/usage_signals.py` (never imported by the app; isolation tests in
`tests/test_usage_signals.py`, `test_independent_projections.py`, `test_weekly_learning.py`) and
`scripts/usage_signals_research.py` (`fetch`, `audit`, `run`, `ablation`, `report`). Outputs go to
`data/models/research/usage_signals/` (git-ignored).

## Phase 0: what already existed (not duplicated)

| Signal | Where | Status before PR 9 |
|---|---|---|
| targets/game, target share, air yards/game, snap share, snap/opportunity trend, xFP and expected league points, opportunity share, prior-season usage, team change, healthy-game usage, role-drop / injury-exit flags | `forecast_features.py` (ifp-1 C, ifp-2 F/E/Q) | in the benchmark |
| snap-share depth order (`depth_rank`, `ahead_unavailable`) | `participation.py` (ipm-1.0.0) | P(appears) model only; no depth charts |
| FTN per-code target counts (`ftn_read{0,1,2}_targets`, DES/CHK/SD) | `app/nfl/sync.py` → `nfl_pbp_player_week`, **live season only** | semantics flagged UNVERIFIED (`app/nfl/ftn.py`, nflverse/nflreadr#325); display only, never in a model |
| depth charts | `nfl_depth_charts` (live season raw), player API / waiver candidate lanes | never in a model, not point-in-time |

The history DB has no FTN or depth charts, so PR 9 keeps its own content-addressed raw snapshots
(FTN 2022-2026, depth 2019-2026, slim play-by-play 2019-2026). Air-yard *share* is not a separate
ifp feature; `air_yards_pg` is, and it is used in the redundancy check.

## Phase 1: FTN read codes, a semantics problem first

The nflreadr dictionary (PR #319, merged 2026-09-01) says `"0"` is the first read from 2023 and
that 2022 primary reads are NA. **The data contradicts it.** Here are REG targets joined to
play-by-play (`audit.json`):

| code | 2022 share | 2025 share | completion | aDOT | screens | throwaways |
|---|---|---|---|---|---|---|
| `"1"` | 53.4% | 58.3% | 0.65 | 10.4 | 0.2-2% | 0.1% |
| `"2"` | 16.1% | 11.4% | 0.60 | 10.8 | <1% | <1% |
| CHK | 14.6% | 13.7% | 0.80-0.82 | 0.5-0.7 | 2-3% | <1% |
| DES | 9.3% | 10.0% | 0.88-0.89 | -2.7 to -3.1 | 85-87% | <1% |
| SD | 5.5% | 6.0% | 0.47-0.50 | 12.4 | <1% | 5% |
| NA (2022) / `"0"` (2023+) | 1.2% | 0.6% | 0.12-0.17 | 4.4-5.0 | 0-1% | 20-28% |

Outside targets, `"0"` tags 26-27k plays a season in 2023-2025: runs, penalties, kickoffs, punts,
field goals, kneels. That is the same population as 2022's 21k NA rows. So in 2023 `"0"` simply
replaced NA as "no read / not applicable", and the progression codes mean the same thing in every
season. No code exists for a third-or-later read. One 2022 value has a leading space (`" CHK"`).
It is stripped.

Two readings are implemented and **never mixed** (`usage_signals.SEMANTICS`):

* **empirical** (primary): `"1"` first read, `"2"` later read, `"0"` no read. Usable for 2022.
* **dictionary** (sensitivity): `"0"` first, `"1"` second, `"2"` third or later. Under this
  reading a 2022 NA target is *primary or missing*. First reads are **unavailable** for every 2022
  team-game with such a target (null, never zero).

CHK, DES and SD are their own classes under both readings, never first reads. A charted target
without a code is `missing`. A target in an uncharted game is `not_charted`. Both stay null in
features.

**The predictive test agrees with the empirical reading.** The same features under the dictionary
reading lose all of their points gain (development −0.002 [−0.012, 0.008]; replay +0.002). Treat
this as supporting evidence only: upstream has not confirmed either reading.

**Joins and availability.**

* FTN charts **every REG game 2022-2026**: 271 / 272 / 272 / 272 and 2026 weeks 1-5 = 79 games.
* There are no duplicate (game, play) ids. Every FTN row matches a play-by-play play.
* 100% of targets in charted games match an FTN row (73,390 targets). Receivers come from
  play-by-play `receiver_player_id` (GSIS), so no name matching is involved.
* `date_pulled` is overwritten whenever nflverse re-pulls a season. For example, all of 2023 shows
  one stamp, 2024-09-06, so it cannot date historical availability.
* In 2026 the current pull came 40.2 h (week 5) and 70.6 h (week 4) after each week's last game.
  A Thursday game kicks off about 72 h after Monday night, so the previous week's charting may not
  be available yet for a Thursday-cutoff forecast. Historical replays assume the prior week is
  available, which is an optimistic assumption. `restrict_to_available()` enforces the strict rule
  for prospective use.

## Phase 2: receiver features

All features come from the player's last ≤5 appearances in usable charted games strictly before
the target week. The window covers the current season plus the previous one. Shrinkage is
conservative, toward the position mean.

| feature | definition |
|---|---|
| `fr_team_share` | player first-read targets / team first-read targets (+10 team first-read targets of prior) |
| `fr_own_share` | share of his charted targets that were first reads (+5 targets of prior) |
| `fr_tpg`, `des_tpg`, `chk_tpg` | first-read / designed / checkdown targets per charted game |
| `fr_share_trend` | last game's first-read share minus the earlier games' mean |
| `fr_minus_ts` | first-read team share minus ordinary target share in the same games |

A player who appeared in a charted game without being targeted has measured zeros. Pre-registered
group **A** is `fr_team_share` and `fr_own_share` only. `A_all` (every feature) is an exploratory
development run.

**Redundancy** (development 2022-23, R² of the feature regressed on target share, targets/game, air
yards/game, expected points, snap share, opportunity trend and opportunity share):

| | WR | TE | RB |
|---|---|---|---|
| `fr_team_share` | 0.92 (r = 0.95 with target share) | 0.91 | 0.48 |
| `fr_own_share` | 0.14 | 0.13 | 0.13 |
| `fr_minus_ts` | 0.26 | 0.13 | 0.84 |

First-read team share for pass catchers is almost a relabelled target share. The novel content is
*how* targets are earned (`fr_own_share`).

**Descriptive vs predictive.** Within the same game, first-read share correlates with fantasy
points (WR r = 0.74, TE 0.74, RB 0.36). Ordinary target share correlates more strongly (0.79 /
0.82 / 0.67). A strong same-game correlation says nothing about next week; only the walk-forward
tests below do.

## Phase 3: timestamped depth charts

* **Modern (2025+)**: 221 / 238 snapshots (about one a day, most at 06-10 UTC), 32 teams.
  Snapshots are filtered to group `3WR 1TE`. `pos_rank` is position-wide (WR ranks interleave the
  three WR slots), so it is re-ranked into an ordinal per team and position. Tiers:
  1 = QB1/RB1/TE1/WR1-3, 2 = next tier, 3 = lower.
* **Point-in-time rule**: the latest snapshot with `dt` *strictly before* the team's kickoff minus
  `cutoff_hours`, at most 8 days old. A later snapshot is never joined, and one exactly at the
  cutoff is excluded (tested).
* Change features compare against the team's previous game's pregame snapshot.
* Each player is looked up on his forecast team's chart. A traded player who is still listed by
  his old team counts as off his new team's chart.
* A chart position that differs from the player's position is an identity conflict and gets null
  features (28 rows). 3.1% of skill rows (9,973) lack a GSIS id and are dropped.
* **Coverage at the 1.5 h cutoff**: a pregame snapshot exists for 99.2-100% of appearances.
  On-team-chart rates: QB 100%, WR 99%, TE 97-99%, RB 87-89% (RBs are often absent). Median
  snapshot age is 9.3 h in 2025 and 3.2 h in 2026; p95 is 16.5 h.
* **Legacy (2019-2024)**: one weekly chart per team-week with no publication time, so being
  pregame cannot be verified. It was **not** used to train or evaluate and **never** backfilled
  from modern snapshots.
  * Timing diagnostic (QB/RB/TE unique starters): legacy starters led the same game in snaps
    74.6% of the time and the previous game 79.7%.
  * Modern pregame snapshots: 85.8% / 85.7%.
  * So legacy charts look stale rather than post-hoc, but their availability time stays unknown.

## Phase 4: reliability and licensing

* **FTN charting** is CC-BY-SA 4.0, unlike most nflverse data (CC-BY 4.0). Attribution: *"FTN
  Data via nflverse"*. ShareAlike means any redistributed derivative (a published feature table or
  dataset) must carry CC-BY-SA 4.0. Using it only to fit an internal model has lower obligations,
  but publishing FTN-derived numbers in the app would need the attribution and an
  share-alike review. Nothing FTN-derived is published by this PR, and raw files stay git-ignored.
* **Modern depth charts** carry ESPN player ids and appear to come from ESPN's public depth charts
  via nflverse. nflverse's license covers its own compilation; the upstream terms for commercial
  reuse are **not verified** here. Treat them as internal research input only until that is
  checked. We do not scrape anything ourselves.
* **Corrections**: `fetch` stores each download as `<family>_<season>-<sha>.parquet`. A changed
  upstream file becomes a new snapshot; the old one is kept in the manifest history. Every cache
  fingerprint covers these snapshot hashes, all source frames, the weeks, the scoring fingerprint,
  the polars version and the builder code. Caches are content-addressed (`signals-<fp>.parquet`),
  and reports refuse to be reused against changed inputs.

## Phase 5: experiments

**Benchmark.** The frozen ifp-2 challenger (same features, penalty and 36-week half-life, refit
walk-forward each week). Our walk-forward reproduces its predictions **exactly**: max |diff| = 0.0
over 12,314 rows. Its published numbers also reproduce: development 4.216 → 4.202 (n 13,372),
confirmation 4.031 → 4.016 (13,345), exploratory 4.068 → 4.050 (8,645).

**Candidates.** ifp-2 features plus the group's features, refit identically. Missing source data
gets ifp's automatic missing indicator, so every model scores **the same rows**. Covered and
uncovered subsets are reported separately.

**Windows** (fixed before results): FTN development 2022-23, already-inspected replay 2024-26 wk 5.
Modern depth: development 2025, replay 2026 wks 1-5. **Neither has an untouched holdout**, and
2025-26 is not one.

**Labels.**

* next-week points (league scoring);
* targets;
* realized expected league points (xFP; a measured zero for a zero-opportunity appearance in a
  covered team-week);
* snap-share role up / down (±15 pp vs the mean of the previous ≤4 games);
* Start/Sit pair accuracy.

### Pooled results (MAE difference vs ifp-2, week-clustered 95% CI; 1.5 h depth cutoff)

| group | window | n | points (ifp-2 MAE) | Δ points | Δ targets | Δ exp. points | pairs ifp-2 → cand |
|---|---|---|---|---|---|---|---|
| A first-read | dev 22-23 | 12,055 | 3.854 | **−0.020** [−0.025, −0.016] | −0.003 [−0.005, 0.000] | **−0.019** [−0.026, −0.012] | 66.40 → 66.37% |
| A | replay 24-26 | 13,803 | 3.799 | **−0.012** [−0.014, −0.011] | −0.004 [−0.005, −0.003] | −0.011 [−0.013, −0.009] | 65.59 → 65.61% |
| A_all (exploratory) | dev | 12,055 | 3.854 | −0.012 [−0.020, −0.003] | −0.003 [−0.007, 0.003] | −0.016 | 66.40 → 66.33% |
| B depth | dev 2025 | 6,720 | 4.008 | **−0.042** [−0.060, −0.023] | −0.020 [−0.029, −0.013] | −0.045 [−0.068, −0.020] | 65.12 → 65.22% |
| B | replay 2026 | 1,925 | 4.195 | **−0.078** [−0.119, −0.041] | −0.022 [−0.031, −0.017] | −0.067 [−0.087, −0.049] | 66.03 → 66.32% |
| C both | dev 2025 | 6,064 | 3.738 | −0.027 [−0.044, −0.009] | −0.021 | −0.040 | 65.60 → 65.72% |
| C | replay 2026 | 1,747 | 3.939 | −0.055 [−0.088, −0.031] | −0.023 | −0.060 | 66.89 → 67.08% |

For scale: ifp-2's whole gain over ifp-1 C was 0.014-0.018. The rejected PR 54-57 candidates
missed by +0.002 (opportunity/efficiency) and +0.004 (HGB) on the replay.

### By position (points Δ)

* **A**:
  * dev: RB −0.028, WR −0.026, TE −0.003 (n.s.)
  * replay: RB −0.014, WR −0.020, TE +0.001
  * TE target MAE gets slightly *worse* (+0.005 / +0.002). The value is WR and RB, not TE.
* **B**:
  * dev: QB −0.195 [−0.32, −0.05], RB −0.066 [−0.14, 0.00], WR −0.024 [−0.05, 0.00],
    TE +0.014 (n.s.)
  * replay: QB −0.346, RB −0.085, WR −0.033, TE −0.045
* **Coverage**: B's 22 uncovered development rows (no usable chart) get slightly worse (+0.026,
  n.s.). The gain is entirely in covered rows. A's uncovered rows (no prior charting) are unchanged.

### Robustness

* **72 h cutoff** (a Thursday-morning snapshot for a Sunday game): B dev −0.037 [−0.052, −0.021],
  replay −0.069 [−0.109, −0.033]. The effect is not game-day news only.
* **Dictionary semantics**: A dev −0.002 (n.s.), replay +0.002.
* **Role-change classifiers** (logistic): A and C are slightly worse on development log loss.
  * B dev: AUC down 0.729 → 0.720 (role down); log loss up a little for role down and down a
    little for role up. Mixed.
  * B 2026: role-down log loss 0.404 → 0.391, AUC 0.731 → 0.746.
  * No group reliably improves role-change classification.

### Ablation (B, development; `ablation_empirical_*.json`)

| model | points MAE | exp. points MAE |
|---|---|---|
| ifp-2 | 4.008 | 3.095 |
| B (all four) | 3.966 | 3.050 |
| B − `dc_tier` | 4.003 | 3.081 |
| B − `dc_tier_change` | **3.961** | 3.047 |
| B − `dc_vacancy` | 3.963 | 3.048 |
| B − `dc_snap_gap` | **3.961** | **3.043** |

The 72 h ablation looks the same. Where B's gain comes from (share of the total absolute-error
reduction):

| cohort | share of gain |
|---|---|
| backup QBs who appeared (161 rows) | **53%** (MAE 6.49 → 5.56) |
| QB starters | −8% |
| players whose tier changed | −3% (−21% at 72 h) |
| a teammate ahead left the chart | −16% (−31% at 72 h) |
| chart vs snap-share conflicts | +14% |
| a same-position teammate Out/Doubtful | +13% |
| none of these | 64% (mostly level, QB backups included) |

The *changes* the spec hypothesized carry no forward value beyond snap trends and actually hurt.
The chart's static statement of who starts carries the value, and it matters most where ifp-2 is
blind: a QB who started recently but is the backup again.

## Phase 6: conclusions per feature

| feature | hypothesis | coverage / validity | incremental value | verdict |
|---|---|---|---|---|
| `fr_team_share` | earning first reads predicts targets | 2022+, every REG game; prior-week charting assumed available (pulls land 40-71 h after MNF) | mostly redundant with target share (R² 0.92 WR) | **continue** (inside A) |
| `fr_own_share` | target *quality* predicts volume | same | WR/RB −0.02 to −0.03 points; low redundancy | **continue**: the novel FTN part |
| `des_tpg`, `chk_tpg`, `fr_tpg`, `fr_share_trend`, `fr_minus_ts` | | | A_all is worse than A (dev −0.012 vs −0.020; TE +0.023) | **reject** (exploratory, development only) |
| `dc_tier` | the chart's starter says who gets the volume | 2025+ only (≈1.3 seasons), pregame snapshots for 99%+ | the entire B gain; robust to a 72 h cutoff | **continue as a new pre-registered candidate** (B-tier) |
| `dc_tier_change`, `dc_vacancy` | promotions / departures add information beyond snap trends | same | removing them improves MAE; vacancy cohort worse | **reject** |
| `dc_snap_gap` | chart vs usage disagreement | same | removing it improves MAE | **reject** |
| legacy depth (≤2024) | | no publication time | not tested | **reject** for modelling (timing unverifiable) |
| FTN under dictionary semantics | | 2022 primary reads unavailable | none | **reject** |

**Overall.** The pre-registered decision rule passes for A, B and C (`report_*.json`). Two caveats
apply:

1. Its replay tolerance (+0.005) is loose relative to these effect sizes.
2. B passes for a reason other than its hypothesis.

So the honest summary is: **one modest FTN signal and one depth-level signal worth a prospective
test; the depth-change hypothesis fails.**

### Engineering cost and data risk

* **FTN**: one extra nflverse download per week, a play-id join and five aggregates. The main
  risks are the unresolved code semantics (a silent upstream relabelling would invert the feature)
  and late pulls before Thursday games.
* **Depth tier**: one snapshot per team per forecast. The risks are ESPN-sourced terms of use,
  ~11% of RBs absent from charts, and only 1.3 seasons of history.

### Follow-up plan (prospective shadow; not part of this PR)

1. Pre-register two shadow variants of ifp-2 and nothing else:
   * **ifp-2 + `fr_team_share`, `fr_own_share`** for RB/WR.
   * **ifp-2 + `dc_tier`** for QB/RB/WR (TE excluded: no development gain).
   * Keep the frozen penalties and half-life.
2. Each week, freeze both at the existing weekly-learning pregame cutoff.
   * Capture FTN with `restrict_to_available(as_of=forecast time)`. If the previous week is not
     charted yet, the feature is missing; never backfill it.
   * Capture the depth snapshot used (sha + `dt`) in source provenance.
3. Judge with the existing `SHADOW_GATE`: ≥ 8 fully final prospective weeks, ≥ 400 matched
   appearances per position, CI upper bound < 0, bias / starters-pool / interval guards.
   2026 weeks 7-14 are the earliest possible evidence.
4. Before anything is consumer-facing: confirm FTN code semantics upstream (nflreadr#325),
   complete a CC-BY-SA review for any published derivative, and check the depth-chart upstream
   terms.

## Reproduce

```bash
cd fantasy-intel/backend
uv run python ../scripts/usage_signals_research.py fetch
uv run python ../scripts/usage_signals_research.py audit
uv run python ../scripts/usage_signals_research.py run                      # empirical, 1.5 h
uv run python ../scripts/usage_signals_research.py run --cutoff-hours 72
uv run python ../scripts/usage_signals_research.py run --semantics dictionary
uv run python ../scripts/usage_signals_research.py ablation [--cutoff-hours 72]
```

Prerequisites: the same as `challenger_research.py`, i.e. the history and live DBs, the frozen
ifp-1.0.0 `selection.json` (scoring fingerprint `e165b59686110c7d`, enforced) and the play-by-play
exit files. A full run takes about 10 minutes on 4 cores.

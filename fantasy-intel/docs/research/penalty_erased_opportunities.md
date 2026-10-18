# Penalty-erased targets and carries (`ifp-pe-0.1.0`, research only)

Date: 2026-10-18. Data: nflverse play-by-play 2019 to 2026 week 6 (`nflreadpy.load_pbp`, cached per season),
nflverse weekly rosters, and the same ifp-2 feature store, fully-final weeks and scoring fingerprint
(`e165b59686110c7d`) as the other ifp-2 studies. Code:

* `backend/app/analytics/penalty_erased.py` (parser, identification, features, candidates, rules)
* `scripts/penalty_erased_research.py` (`identify`, `build`, `develop`, `evaluate`)
* tests: `backend/tests/test_penalty_erased.py`

**Question.** When a penalty wipes out a snap ("- No Play"), the target or carry disappears from the
official stat line and from our play-by-play aggregates, even though it shows who the offense was
trying to get the ball to. NFL Savant reports about 1,000 such uncredited targets a season. Do they
improve next-week projections if we count them back in?

**Answer: no.** Verdict **`retain_baseline`**.

* **Identification works.** We find 880 to 1,075 erased targets and 306 to 443 erased carries per full
  season, which matches Savant's figure. Every one gets a confident player id. Scored against
  225,241 official plays where nflverse supplies the id, the method is 99.999% precise with 99.6% recall.
* **The features barely move the opportunity measures.** Adjusted and official shares correlate at
  0.997 or higher for RB, WR and TE. The 90th-percentile shift in target share is 1.0 percentage
  points for WRs.
* **Neither pre-registered candidate qualifies at any position on development 2021-22.** The best
  is WR "V" at −0.002 MAE [−0.005, +0.001], against a bar of −0.010 with a CI upper bound below 0. The frozen
  selection is ifp-2 everywhere, so the rules give `retain_baseline`.
* **The descriptive 2023-24 and 2025-26 replays agree.** Every position-window difference is
  between −0.003 and +0.006 MAE. Start/Sit pairs and the starters pool are unchanged.
* **The erased volume carries a small mean signal but no MAE gain.** ifp-2 under-projects players
  with ≥ 2 erased opportunities in their window by up to about half a point. Both candidates shrink that
  bias, but MAE rises on those rows and falls on the rows without erased plays, netting to zero.
  This is the same one-sided-feature pattern as `returning_teammate_features.md`.

ifp-1.0.0, ifp-2.0.0, the registry, the weekly pipeline and ledgers, Start/Sit, the API and official
fantasy scoring are unchanged. Nothing in `app/` imports the module (tested).

## 1. Identifying erased plays

nflverse keeps no-play snaps as `play_type == "no_play"`, but it **nulls `receiver_player_id`,
`rusher_player_id` and `air_yards` on them**. Only two-point tries keep the ids. Our official
aggregates (`sync.classify_plays`, `nfl_weekly_stats`) therefore drop these snaps, and so must the
desc parser recover the player:

1. **Scope.** REG season, `play_type == "no_play"`, not a two-point try. nflverse does not always set
   `two_point_attempt` on a nullified try, so a desc starting "TWO-POINT CONVERSION ATTEMPT" is also
   excluded.
2. **Parse only the ruled play.** Take the text before the first "PENALTY on" (so a player named in the
   penalty is never the recipient) and after the last replay "REVERSED.".
3. **Erased target:** `NN-Name pass [incomplete] [short|deep] [left|middle|right] (to|intended for) NN-Name`.
   This covers complete, incomplete and intercepted passes. A pass with no named receiver ("thrown away") is not a target.
4. **Erased carry:** `NN-Name [scrambles] (left|right) (end|tackle|guard) | up the middle`. Kneels,
   spikes, sacks and aborted snaps are not carries. Scrambles are carries, as in `classify_plays`.
5. **Resolve the jersey on the offense (`posteam`) with two independent sources:**
   * the **roster** source: nflverse weekly roster (season, week, team, jersey) → gsis_id;
   * the **game** source: the same game's official plays, which carry `*_player_id` and `*_jersey_number`
     together.

   A play is `confident` when at least one source resolves the jersey, the abbreviated desc name
   matches the surname (the prefix rule of `pbp_exits`), and the sources do not disagree. Any other
   play keeps its failure reason (`no_posteam`, `no_jersey_match`, `name_mismatch`,
   `source_conflict`) and never enters a feature.
6. **DPI** is flagged per play. Its penalty yards (the spot of the foul) stand in for air yards. Other erased
   targets get fixed air-yard constants measured on 2019 official targets: short 4.37, deep 25.11,
   no depth given 8.25. 2019 precedes every training and test week, so these constants carry no leakage.

**Counts (2019 to 2026 week 6):**

| Season | Erased targets | of which DPI | Erased carries | Receiver-less passes |
|---|---|---|---|---|
| 2019 | 1,075 | 278 | 425 | 60 |
| 2020 | 886 | 312 | 306 | 52 |
| 2021 | 980 | 305 | 400 | 53 |
| 2022 | 884 | 235 | 380 | 45 |
| 2023 | 880 | 272 | 361 | 62 |
| 2024 | 1,017 | 314 | 443 | 73 |
| 2025 | 932 | 285 | 401 | 64 |
| 2026 wk 1-6 | 331 | 96 | 171 | 30 |

* **Totals.** 6,985 targets and 2,887 carries; 473 of the carries are scrambles.
* **Penalty side.** Targets: 4,320 defensive, 2,490 offensive, 175 offsetting. Carries: 2,343 offensive
  (mostly holding), 456 defensive, 88 offsetting.
* **Penalty type** (nflverse `penalty_type` records the first penalty only). Most common for erased
  targets: DPI 2,032, offensive holding 1,019, defensive holding 718, OPI 522, defensive offside 482.

**Identification rate.** All 9,872 erased targets and carries get a confident id:

* 9,552 with both sources agreeing;
* 288 from the roster source only;
* 32 from the game source only;
* 0 conflicts.

**Validation against ground truth (`validate_on_official`).** On official targets and carries (no
penalty, no sack, kneel, spike or two-point try), nflverse supplies the true id. So the same parser
and resolver were scored on all of them. The game source is leave-one-out: the play's own observation
is removed, as it is absent on a no_play row.

| | Plays | Correct | Wrong | Unresolved | Not parsed |
|---|---|---|---|---|---|
| Combined (as used) | 225,241 | 224,419 | 2 | 25 | 795 |
| Roster source alone | | 223,509 | 2 | 935 | |
| Game source alone (leave-one-out) | | 218,436 | 2 | 6,008 | |

Precision is 99.999% and recall 99.6%, and both are at least 99.5% in every season. **Failure modes:**

* **The 2 wrong ids are laterals.** The desc names the first ball carrier, while nflverse credits the
  lateral receiver.
* **The 795 unparsed plays are aborted snaps and QB recoveries.** The desc has no run direction
  ("9-B.Young to ATL 38 for -5 yards"). These are not designed runs, and on nullified snaps they are rare.
* **The 25 unresolved plays are all 2019 roster gaps.** The weekly roster lacks the jersey, or a
  different player wears it. 926 of the 935 roster-unresolved plays are 2019. The game source covers most of them.
* **The game source alone misses players credited only once in a game.** That is why both sources are used.
* **DPI coverage.** 2,097 of 2,105 no-play DPI snaps (99.6%) parse as targets. The rest name no
  receiver (`to [86]`, or "pass incomplete short middle." with no "to").
* **Other no-play snaps whose ruled play names a ball carrier.** Of 10,419 such snaps, 78 are not
  parsed, after excluding sacks, kneels and spikes. All 15 sampled are punts or aborted snaps, none a
  missed target or carry.
* **Spot check.** A random sample of 25 erased plays was read by hand against the resolved player.
  All 25 were right, including suffix names ("K.Hill Jr."). A fixed 40-play sample is written to
  `identification.json` for re-checking. This spot check found the two-point leak described above,
  which was then fixed and tested.

## 2. Point-in-time features

Features are built per player and week (S, W) from games strictly before (S, W). They use the
current and previous season and the player's last 5 games, the same window as
`forecast_features.features_for_week`. The adjusted and official quantities therefore differ **only**
by erased plays.

| Feature | Definition |
|---|---|
| `pe_tgt_pg`, `pe_car_pg`, `pe_air_pg`, `pe_dpi_pg` | erased volume per game (confident ids only) |
| `pe_adj_targets_pg`, `pe_adj_carries_pg`, `pe_adj_air_yards_pg` | official per game + erased per game |
| `pe_adj_target_share` | (targets + erased) / (team targets + team erased targets) |
| `pe_adj_rush_share` | (carries + erased) / (team rush attempts + team erased carries) |
| `air_share`, `pe_adj_air_share` | receiving air yards / team air yards, official and adjusted |
| `pe_rate` | erased / (official + erased) targets + carries |

* **Team totals include every erased play the offense attempted**, resolved or not, so the denominators
  are complete.
* **Missing data.** A game with parsed play-by-play and no erased play is a measured 0. A team-week
  without parsed play-by-play is null.
* **Coverage.** Panel team-week coverage is 99.95%. Features are null for 2.1-2.7% of rows in
  2020-25 (week 1, players new to the league) and 4.8% in 2026.
* **Official scoring is never changed.** Labels stay official league points. The module reads
  neither labels nor scoring (tested).
* **Leakage contract (tested).** Adding week-W and later plays never changes week-W features.

**How much the adjustment moves things** (2020 to 2026 rows):

| | RB | WR | TE |
|---|---|---|---|
| Erased targets per game, mean (p90) | 0.07 (0.2) | 0.24 (0.6) | 0.11 (0.4) |
| Erased carries per game, mean (p90) | 0.17 (0.5) | — | — |
| `pe_rate` mean (p90) | 2.6% (6.3%) | 5.4% (13.0%) | 4.2% (11.8%) |
| Correlation, adjusted vs official target share | 0.997 | 0.998 | 0.998 |
| Correlation, adjusted vs official rush share | 0.9997 | — | — |
| p90 absolute share shift | 0.6 pp target, 0.9 pp rush | 1.0 pp target | 0.7 pp target |
| Rows with ≥ 2 erased opportunities in the window (`pe_heavy`) | 3,268 | 5,156 | 1,386 |

Erased plays are spread almost in proportion to official volume. The adjustment is therefore close
to a rescaling of shares the model already has.

## 3. Pre-registered design (`penalty_erased.DECISION_RULES`)

The module, script, tests and rules were committed in `cc183cc` **before any points backtest was run**.

**Two candidates, both on ifp-2's frozen spec** (features, penalty and recency weights). QB always
keeps ifp-2.

* **A, "adjusted".** Replace every official opportunity measure with its penalty-adjusted counterpart.
  * RB: `targets_pg`, `carries_pg`, `target_share`, `rush_share`.
  * WR and TE: `targets_pg`, `target_share`, `air_yards_pg`.

  There is no new free parameter: an erased play counts exactly like a credited one.
* **V, "volume".** ifp-2 plus erased volume, so the ridge may weigh an erased play differently.
  * RB: `pe_car_pg`, `pe_tgt_pg`, `pe_rate`.
  * WR and TE: `pe_tgt_pg`, `pe_air_pg`, `pe_rate`.

**Selection (development 2021-22 only, per position).** A candidate qualifies if both hold:

* MAE − ifp-2 ≤ −0.010, with a week-clustered bootstrap 95% CI upper bound < 0;
* |bias| is not worse by more than 0.15.

The lower development MAE among qualifying candidates is adopted.

**Confirmation (2023-24, run once).** Checks c1-c6 are as in `returning_teammate_features.md`:

1. pooled MAE diff < 0 with CI upper bound < 0;
2. no adopted position significantly worse;
3. RMSE not worse by > 0.5%;
4. |bias| not worse by > 0.15;
5. starters-pool MAE not worse by > 1%;
6. Start/Sit pair accuracy not lower by > 0.5 pp.

Mechanism check c7: MAE diff < 0 on each adopted position's `pe_heavy` rows.

**Outcome.** If every check passes, the outcome is `adopt_candidate`. That is a recommendation to
pre-register a frozen pregame shadow ledger and changes nothing by itself. Any failure, or nothing
adopted, gives `retain_baseline`.

**Reference models.** The 2025-26 replay, the trailing baselines A1 (last-5 average) and A2 (season
average), and ESPN are descriptive only.

**Two changes were made before any points backtest, and are disclosed here:**

* The two-point desc filter, found in the identification spot check.
* A descriptive-only cohort, `pe_rate_10` (≥ 10% of window opportunities erased). It was added after
  looking at cohort sizes, not labels.

## 4. Results

Identical rows, week-clustered 95% CIs with 1,000 resamples. Differences are candidate − ifp-2 MAE; negative is better.

**Development 2021-22 (selection).** Rows: RB 3,557, WR 5,324, TE 3,211; pooled RB+WR+TE n = 12,092.

| Position | ifp-2 MAE / RMSE / bias | A − ifp-2 | V − ifp-2 | A1 − ifp-2 | A2 − ifp-2 | Qualifies |
|---|---|---|---|---|---|---|
| RB | 4.296 / 6.021 / −0.117 | −0.000 [−0.002, +0.002] | +0.008 [−0.002, +0.016] | +0.216 | +0.106 | none |
| WR | 4.438 / 6.057 / −0.281 | −0.001 [−0.002, +0.000] | −0.002 [−0.005, +0.001] | +0.103 | +0.068 | none |
| TE | 2.990 / 4.467 / −0.060 | −0.001 [−0.003, +0.001] | +0.001 [−0.008, +0.011] | +0.102 | +0.062 | none |
| RB+WR+TE | 4.011 / 5.667 / −0.174 | −0.001 [−0.002, +0.000] | +0.002 [−0.002, +0.006] | +0.136 | +0.078 | |

* Frozen write-once selection: `frozen_selection_ifp-pe-0.1.0.json`, every position `ifp2`.
* Starters pool, A / V − ifp-2: RB −0.001 / +0.004, WR −0.000 / −0.002, TE +0.001 / +0.001. No CI excludes 0.
* Start/Sit pairs (accuracy / mean regret): ifp-2 65.37% / 2.523, A 65.39% / 2.521, V 65.37% / 2.523.
  A1 and A2 are much worse (62.4% / 2.891 and 63.1% / 2.803).

**Replays (descriptive: nothing was adopted, so they decide nothing).** Each cell is A − ifp-2 / V − ifp-2.

| Position | 2023-24 (n) | 2025-26 (n) |
|---|---|---|
| RB | −0.001 / +0.006 [−0.001, +0.012] (3,398) | −0.001 / −0.003 [−0.009, +0.003] (2,196) |
| WR | −0.000 / −0.001 [−0.006, +0.004] (5,409) | −0.000 / +0.001 [−0.005, +0.007] (3,477) |
| TE | +0.000 / +0.000 [−0.008, +0.008] (3,208) | −0.003 [−0.005, −0.000] / −0.001 (2,138) |
| RB+WR+TE | −0.000 / +0.001 (12,015) | −0.001 / −0.001 (7,811) |
| ifp-2 MAE / RMSE / bias, pooled | 3.791 / 5.468 / +0.033 | 3.783 / 5.480 / −0.030 |
| A1 / A2 − ifp-2, pooled | +0.129 / +0.064 | +0.144 / +0.127 |
| Start/Sit pairs: ifp-2 / A / V | 65.57% / 65.57% / 65.58% | 65.32% / 65.36% / 65.36% |

* The 2025-26 TE result for A (−0.003, CI upper bound −0.000) is the only interval that excludes 0. It
  is far below the −0.010 bar and was not selected on development, where it was −0.001.
* By season (pooled RB+WR+TE): 2025 is A −0.001, V −0.001; 2026 weeks 1-5 are A −0.002, V −0.000.

**ESPN.** No comparison is possible yet. The live database's pregame ESPN snapshots start at 2026
week 6, which is not fully final, so no ESPN row overlaps a scored row. This is a data gap, not a
result, and nothing about ESPN is claimed here. ifp-2 itself is not shown to beat ESPN
(`independent_projection_engine.md`).

## 5. Why it does not help: mechanism cohorts

In the table below, bias is actual − predicted, so positive means under-projection. Each bias cell
is ifp-2 / A / V. Each "MAE diff" cell is A / V − ifp-2.

| Window | Position | `pe_heavy` n | Bias, `pe_heavy` | MAE diff, `pe_heavy` | MAE diff, no erased play |
|---|---|---|---|---|---|
| dev | RB | 1,058 | −0.12 / −0.16 / −0.26 | +0.007 / +0.034 | −0.006 / −0.009 |
| dev | WR | 1,626 | −0.22 / −0.24 / −0.25 | +0.005 / +0.008 | −0.004 / −0.016 |
| dev | TE | 448 | +0.51 / +0.44 / +0.14 | +0.014 / +0.081 | −0.005 / −0.028 |
| 2023-24 | RB | 1,034 | +0.08 / +0.05 / −0.02 | +0.005 / +0.018 | −0.006 / −0.017 |
| 2023-24 | WR | 1,718 | +0.25 / +0.18 / +0.08 | +0.010 / +0.028 | −0.011 / −0.028 |
| 2023-24 | TE | 408 | +0.57 / +0.49 / +0.22 | +0.010 / +0.037 | −0.004 / −0.021 |
| 2025-26 | RB | 722 | +0.40 / +0.36 / +0.30 | +0.004 / +0.007 | −0.005 / −0.016 |
| 2025-26 | WR | 1,040 | +0.09 / +0.02 / −0.05 | +0.013 / +0.025 | −0.008 / −0.010 |
| 2025-26 | TE | 327 | +0.28 / +0.20 / +0.08 | +0.002 / +0.020 | −0.006 / −0.012 |

* **Most of the mechanism-cohort MAE differences are worse, with CIs above 0.** So the pre-registered
  mechanism (c7) would have failed for every position, had any been adopted.
* **There is a real but tiny mean signal.** After 2022, ifp-2 under-projects heavy-erased players,
  and the candidates (V most) shrink that bias. Erased targets per game correlate +0.01 to +0.05 with ifp-2's residual on the
  2023-26 rows.
* **It does not translate to absolute error.** Weekly points are right-skewed. Lifting the projection
  of a heavy-erased player moves the mean toward the actual mean but away from the typical outcome, so
  MAE rises on those rows. The fit pays for it by lowering every player without an erased play, which
  is where all the MAE gains sit.
* **The net is zero, in every window.** Erased volume is one-sided (always ≥ 0) and mostly
  proportional to credited volume, so a single linear fit cannot isolate the small signal it carries.
* **Splitting out DPI does not change this.** Rows whose window drew a DPI (`pe_dpi`) are worse
  under both candidates in development and confirmation, for example WR +0.004 / +0.010 and TE
  +0.010 / +0.076 in development.

## 6. Recommendation

**Retain ifp-2. Do not add penalty-erased features to the projection model.**

* **Do not pre-register A or V as a shadow.** Neither cleared development. The replays, starters pool
  and Start/Sit all agree that there is nothing to gain.
* **The reusable part is the parser and its validation.** If a future display wants Savant-style
  "true targets" (official + erased), `penalty_erased.derive` gives them with measured accuracy
  (99.999% precision against nflverse ids). It must stay a descriptive stat, not a model input,
  unless a new pre-registered study shows otherwise.
* **Do not retry this as a cohort-only adjustment.** That was the follow-up `ifp-ret` proposed. Here
  the cohort itself gets worse on MAE, so there is no cohort gain to protect.
* **ESPN.** Revisit only as part of the general ESPN benchmark, once pregame ESPN snapshots overlap
  fully-final weeks. Nothing here depends on it.

## 7. What changes, and what does not

**Research only:**

* the module, its script, its tests and this report;
* the two isolation allow-lists in `tests/test_independent_projections.py` and
  `tests/test_weekly_learning.py`, which name the module;
* a test asserting that nothing in `app/` imports it and that it writes nothing.

`tests/test_injury_workload.py` needs no entry: the module never names the injury modules.

**Unchanged:**

* ifp-1.0.0 and ifp-2.0.0 artifacts and config;
* the model registry, production projections, the weekly pipeline and ledgers;
* official scoring, the ESPN/Ours switch, Start/Sit, the API, schedules and dependencies.

Outputs go to `data/models/research/penalty_erased/` (git-ignored): play-by-play cache,
`plays.parquet`, `identification.json`, the store, `development.json`, the frozen selection and
`evaluation.json`.

## Reproduce

Each step takes seconds to a few minutes on the local machine once play-by-play is cached. The store
build reuses the HGB study's builder in its own cache directory.

```
cd backend
uv run python ../scripts/penalty_erased_research.py identify    # parse + id validation report
uv run python ../scripts/penalty_erased_research.py build       # ifp-2 store + erased-opportunity features
uv run python ../scripts/penalty_erased_research.py develop     # 2021-22 only; writes frozen_selection_ifp-pe-0.1.0.json once
uv run python ../scripts/penalty_erased_research.py evaluate    # 2023-24 + 2025-26, ESPN where pregame snapshots exist
```

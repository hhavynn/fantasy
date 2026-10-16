# QB and teammate context challenger (ifp-ctx, research only)

**Question.** Do point-in-time features describing *who the expected starting QB is* and *which
teammates are unavailable or returning* make ifp-2's conditional projections more accurate,
especially in the weeks where those circumstances change?

**Short answer.**

* **Expected starting QB can be identified before kickoff, and its quality matters most for QB rows.**
  * Pregame identification picks the actual starter 95.8% of the time when the evidence class is
    `supported` (2,761 team-weeks). It is 68.7% for `probable` and 56.1% for `uncertain`.
  * Adding QB context to the QB model lowers QB MAE in all three windows: development -0.146
    [-0.273, -0.003], confirmation -0.154 [-0.284, -0.014], replay -0.288 [-0.400, -0.182]. That is
    2.4-4.4% of QB MAE, far larger than any earlier candidate.
  * The gain is concentrated where the QB situation changed: about -0.9 to -1.2 MAE on QB rows in the
    changed-QB cohort.
  * It was **not adopted**. The pre-registered development rule also required |bias| to worsen by no more
    than 0.15, and it worsened by 0.27 (-0.525 to -0.792).
* **QB change for receivers: real but small.**
  * WR rows with a changed expected QB improve by 0.03-0.06 MAE in every window (CIs below 0).
  * Rows without a change are flat or marginally worse, so the pooled WR/RB effect is about 0.
* **Teammate context: the composite passed six of seven confirmation checks and failed the mechanism
  check.**
  * RB and WR adopted A+B on development.
  * On 2023-24, pooled MAE over those positions improves by -0.019 [-0.032, -0.006], with RMSE, bias,
    starters-pool and Start/Sit guards all passing.
  * But on the cohorts the teammate group exists for (top target competitor out, RB1 out), RB MAE is
    *worse* (+0.145, +0.122). That repeats the earlier `tm_transfer` finding. **Verdict:
    `retain_baseline`.**
* **Why the teammate-out cohorts fail on MAE: a mean-vs-median effect, not a wrong direction.**
  * ifp-2 *under*-projects those players (bias +0.7 to +1.8), and the challenger removes most of that bias.
  * RMSE improves on those cohorts in every window (RB1 out: 7.22 to 6.97 in confirmation, 6.63 to 6.15
    in the replay).
  * Proportional redistribution gets the average uplift right, but which teammate realizes it is
    right-skewed and unpredictable, and MAE rewards the median.
* **Returning teammates are the most reliable teammate signal.** With the top target competitor returning,
  MAE improves by -0.06 to -0.08 pooled and by -0.12 to -0.19 for WRs, in every window.
* **Environment (D: PROE, opponent pressure, positional targets allowed) adds nothing reliable.**
* **Nothing is promoted.** A separately pre-registered prospective candidate (`ifp-ctx-p1`: QB with QB
  context, RB/WR with QB + teammate context) gets a write-once pregame research ledger. Only frozen
  forecasts from 2026 week 7 onward can confirm it.

Code: `backend/app/analytics/roster_context.py` (features), `backend/app/analytics/context_challenger.py`
(candidates, walk-forward, cohorts, pre-registered rules, explanation metadata),
`scripts/context_research.py` (`build`, `develop`, `evaluate`, `cases`), tests in
`backend/tests/test_roster_context.py`. Outputs go to `data/models/research/context/` (git-ignored).
Nothing in the app imports these modules (tested). ifp-1.0.0, ifp-2.0.0, the model registry, production
runs, the frozen ledgers, the ESPN/Ours switch, Start/Sit, trades and waivers are untouched.

## 1. Audit of the production model (Phase 0)

**Production is `ifp-2.0.0`** (`model_registry.json`: `production = ifp-2.0.0`, owner override recorded
2026-10-16; `ifp-1.0.0` is the rollback target and fallback). Per week it is retrained by
`weekly_learning.py` from `challenger_config.json`:

* **How points are projected.** Per position, a ridge regression (`challenger.fit_linear`) on
  standardized point-in-time features (`forecast_features.features_for_week`, games strictly before the
  target week). Nulls are imputed with the training mean and paired with a `__missing` indicator, the
  prediction is floored at 0, and players without a game in the last five are abstentions. RB/WR/TE
  weight training rows with a 36-week half-life. Penalties: QB 0.01, RB 0.01, WR 0.03, TE 0.03.
  The estimate is **conditional on appearing**. `expected_points = p_play x projection` uses the
  separate participation model (`participation.py`, `ipm-1.0.0`).
* **Features actually used** (`challenger_config.json` -> `challenger.feature_set`):
  * QB: model C's context features. Opportunity: `xfp_recent`, `pass_att_pg`, `carries_pg`, `snap_pct`,
    `rz_opps_pg`, `games_recent` and missing flags. Production: `pts_recent`, `pts_l3`, `pts_season`,
    `pts_prev_season`, `pts_std`, `pts_minus_xfp`. Plus `snap_trend`, `opp_trend`, `weeks_since_last`,
    `games_season`, `team_changed`, `team_pts_pg`, `opp_pos_allowed`, `is_home`, `opp_pass_epa`,
    `opp_rush_epa`.
  * RB/WR/TE: the same context block with position-specific usage (targets, carries, shares, air yards,
    goal-line), opponent EPA, plus groups F (game-class flags), E (`xfpl_eb`, `cur_h_games`) and Q
    (`prev_xfpl_pg`, `prev_team_changed`).
* **Collected but not used in projections.** QB EPA and CPOE (`nfl_weekly_stats.passing_epa/
  passing_cpoe`), sacks, the starting QB of every game (`nfl_schedules.home_qb_id/away_qb_id`), team
  PROE / neutral pass rate / pressure (`nfl_team_week`), positional targets allowed (`nfl_defense_week`),
  FTN read codes, PFR pressure, NGS, 2026 depth charts and weekly roster statuses (used only by
  participation).
* **Where QB identity, teammate availability and depth are handled today.** Nowhere in the conditional
  points model. A QB change reaches it only after the fact, through the player's own recent production.
  Teammate availability enters only through the player's own role trend. The week-W injury report and
  weekly roster status are inputs to *participation* (p_play), never to conditional points. Depth charts
  are research-only (`usage_signals.py`).
* **Already tried.**
  * `tm_transfer` (`rb_projection_diagnostics.md`): RB teammate Out/Doubtful carries, targets and xFP.
    It passed its RB rule at the effect floor, but its gain did not come from the teammate-out mechanism,
    and the teammate-out cohort was *significantly worse*.
  * Depth-chart changes (`first_read_depth_chart_signals.md`): rejected. All value came from the static
    tier, half of it from backup QBs.
  * FTN first-read share: small, WR/RB, prospective shadow.
  * Rejected: team-volume group K, efficiency split X, healthy-usage U, Huber loss, HGB, adaptive
    workload, opportunity + efficiency.
  * This study therefore uses a different teammate formulation (a team opportunity budget, with
    returning players and gone or reserve teammates) and adds an explicit mechanism check to its rules.

## 2. Data inventory (Phase 4)

Every source below is already ingested from nflverse (`nflreadpy`). **No new feed was added.**

| Signal | Table | Coverage | Pregame? | Used as |
|---|---|---|---|---|
| Starting QB of past games | `nfl_schedules.home/away_qb_id` (gsis ids) | 2019-2026, 23 of 1,960 historical starts lack a matching stats row | a postgame fact about *that* game; the week-W starter is **never** read (tested) | previous starter, starts, recent QB environment |
| QB passing (EPA, CPOE, attempts, sacks) | `nfl_weekly_stats` | 2019-2026 | games before W only | shrunk QB profiles |
| Injury report | `nfl_injuries` | 2019-2026 | week-W report. 2019-2024 rows carry `date_modified`, and rows modified at or after kickoff are dropped. **2025-2026 rows have no timestamp: retrospective** | QB availability, teammate availability |
| Weekly roster status | `nfl_rosters_weekly` | 2019-2026 | weeks before W only (week-W ACT/INA is the game-day decision) | gone / reserve teammates, candidate QBs |
| Box scores, snaps, team totals | panel (`load_panel`) | 2019-2026 | before W | teammate windows |
| PROE, pressure, positional targets allowed | `nfl_team_week`, `nfl_defense_week` | 2019-2026 | before W | Candidate D only |

Not used, on purpose:

* **Depth charts.** No publication time before 2025, and the upstream ESPN terms are unverified for
  commercial reuse (`first_read_depth_chart_signals.md`).
* **FTN read codes.** CC-BY-SA, and the `read_thrown` semantics are unresolved.
* **Closing betting lines.** Set after the forecast cutoff.
* **Raw play-by-play QB-receiver pairs** (Phase 1D). Only per-player aggregates are stored. Pair-level
  chemistry would need a new ingestion of `load_pbp` per season (free, same licence as the existing
  play-by-play derivation, about 1.5 GB a season in memory) and is left as a follow-up. Team-level QB
  quality is tested first, as the brief asks.

Licence: nflverse data is CC-BY 4.0 (FTN excluded). Everything here is derived internally and nothing
is published.

## 3. Feature definitions (fixed before the first backtest)

All features are in `roster_context.CONTEXT_DOCS`. Constants are fixed, never tuned.

**Expected starting QB** (`expected_starters`). Starts from the team's previous starter, then applies
week-W pregame evidence:

| Situation | Expected QB | Evidence class |
|---|---|---|
| Previous starter not listed, no takeover last game | previous starter | `supported` |
| Previous starter Questionable | previous starter | `probable` |
| Another QB out-threw him (>= 10 attempts) last game | previous starter (alt: reliever) | `uncertain` |
| Incumbent with more starts back (listed without designation, or inactive not reserve, last week) | incumbent | `uncertain` |
| Incumbent Questionable | previous starter (alt: incumbent) | `uncertain` |
| Previous starter Out / Doubtful / reserve / gone | best available QB (relief attempts last game, starts this season, team dropbacks) | `probable` with relief or start evidence and no tie, else `uncertain`. Always `uncertain` if Doubtful |
| No game this season / starter id missing / no candidate | none | `unknown` |

Without a week-W report nothing is `supported`. No probability is invented. Section 5 reports the
measured start rate of each class.

**QB profile.**
* EPA per dropback (attempts + sacks) and attempt-weighted CPOE over the current season plus two
  previous seasons, before W.
* Shrunk toward a **replacement-level prior**: pooled rates of completed QB-seasons with fewer than 200
  dropbacks. So a 15-attempt backup is pulled toward backups, not toward league starters.
* Shrinkage is 200 dropbacks / 200 attempts. That is the sample at which per-play noise (EPA SD about
  1.4, CPOE about 45 pts) equals the between-QB spread (about 0.1 EPA, about 3 CPOE points), so
  reliability is 0.5.
* A known QB with no history gets the prior with 0 dropbacks. An unknown QB gets null.

**QB change** (WR/TE/RB rows).
* `qb_epa_delta` / `qb_cpoe_delta`: expected QB's profile minus the mean profile (as of W) of the
  starters in the player's last five games.
* `qb_changed`: the expected QB differs from the most frequent recent starter.
* QB rows get `qb_is_expected` and `qb_self_epa` instead.
* No player id enters a model.

**Teammate context** (all positions; current season).
* Computed over the team games the player played among the team's last five.
* **Vacated**: the share of team targets (carries) that went to teammates unavailable in W. That means
  Out/Doubtful on the W report, off the team, or a non-active roster status before W.
* **Returning**: available teammates who missed some of those games, measured as share-when-playing x
  fraction missed. Positive pregame evidence is required for anyone absent last game, and anyone whose
  last game is older than 6 weeks is ignored.
* **Gain per game**: `(s / (1 - vacated + returning) - s) x team volume per game`. This is a **team
  opportunity budget**: available players' expected shares sum to 1, and an unavailable player receives
  nothing (`redistribute`, tested).
* The ridge coefficient on the gain decides how much of the proportional redistribution actually happens.
  Nothing forces it to be 1.
* Without a report, these are null (never 0).

**Candidate D (environment).** Team PROE (shrunk by 150 plays), opponent defense hit-or-sack rate
(100 dropbacks) and opponent positional target share allowed (100 targets), relative to league.

## 4. Protocol (pre-registered in `context_challenger.DECISION_RULES` before the first run)

Candidates are ifp-2's exact position spec plus one group, with ifp-2's frozen penalty and recency
weights:

* **A**: QB context
* **B**: teammate context
* **C**: A + B
* **D**: environment

No penalty or hyperparameter is searched. Walk-forward uses `challenger.train_rows` (strictly earlier
weeks, asserted). The ifp-2 column is tested to equal `challenger.challenger_predictions` exactly. Each
candidate is compared with ifp-2 on identical rows, with week-clustered bootstrap CIs (1,000 resamples).

Windows:

* **Development 2021-22**: selection only.
* **Confirmation 2023-24**: run once. It was inspected by earlier studies of *other* features, so it is
  not a pristine holdout.
* **Exploratory 2025-26**: already inspected, and its injury reports are untimestamped. Reported only.

One change to the identification rule was made after looking at data, before any points backtest. The
first build showed that 40 of the 94 `supported` misses in 2020-22 were an injured incumbent returning.
The incumbent rule above was added, and its class is labelled `uncertain` because it starts only about
half the time. Points labels played no part in it.

## 5. QB identification (build diagnostics, all seasons 2020-2026, team-weeks)

| Evidence class | Team-weeks | Expected QB actually started |
|---|---|---|
| `supported` | 2,761 | 95.8% |
| `probable` | 166 | 68.7% |
| `uncertain` | 237 | 56.1% |
| `unknown` (mostly week 1) | 224 | no expected QB |

By reason:

| Reason | Team-weeks | Expected QB started |
|---|---|---|
| Previous starter Out (replacement chosen) | 88 | 83% |
| Previous starter Questionable | 104 | 59% |
| Previous starter Doubtful | 20 | 60% |
| Relieved last game | 74 | 55% |
| Incumbent returning | 72 | 50% |
| Incumbent Questionable | 41 | 51% |

The actual starter differed from the previous game's starter in 272 of 3,164 team-weeks. The labels are
ordinal and the start rates above are measured, not assumed. No probability-weighted scenario is used,
because the `uncertain` class (56%) would need a calibrated two-QB model to support one. That is listed as
next work.

Missing data:

* 5.9% of 2020-25 rows have no expected QB (week 1, a missing starter id, or no candidate).
* 10-11% have no teammate window (week 1, or a player new to his team).
* Every week had an injury report, but 2025-26 reports are untimestamped.

## 6. Development (2021-22) and the frozen selection

Penalties and recency weights are ifp-2's. Identical rows (n = 13,372). Week-clustered 95% CIs.

| Position | ifp-2 MAE | A (QB) | B (teammates) | C (A+B) | D (env) | Adopted |
|---|---|---|---|---|---|---|
| QB | 6.008 | -0.146 [-0.273, -0.003] | -0.000 | -0.151 [-0.274, -0.012] | -0.059 [-0.124, 0.000] | **ifp2** (A/C fail the bias guard: -0.525 to -0.79) |
| RB | 4.296 | -0.000 | -0.031 [-0.049, -0.012] | **-0.035 [-0.057, -0.015]** | -0.003 | **C** |
| WR | 4.438 | +0.002 | -0.016 [-0.028, -0.003] | **-0.017 [-0.031, -0.002]** | -0.004 | **C** |
| TE | 2.990 | +0.000 | +0.001 | +0.001 | +0.002 | **ifp2** |

Frozen write-once: `frozen_selection_ifp-ctx-0.1.0.json` (QB ifp2, RB C, WR C, TE ifp2).

## 7. Confirmation (2023-24, run once) and the 2025-26 replay

**Benchmark (confirmation 2023-24, frozen composite `ctx` vs ifp-2, identical rows, n = 13,345, 36 weeks).**

| Position | Baseline MAE | Challenger MAE | Difference | RMSE (base -> ctx) | Bias (base -> ctx) | 95% CI (week-clustered) |
|---|---|---|---|---|---|---|
| QB | 6.056 | 6.056 | 0.000 (not adopted) | 7.600 -> 7.600 | +0.167 -> +0.167 | n/a |
| RB | 4.006 | 3.993 | -0.014 | 5.676 -> 5.651 | -0.020 -> -0.035 | [-0.034, +0.009] |
| WR | 4.211 | 4.188 | -0.023 | 5.961 -> 5.936 | +0.034 -> +0.011 | [-0.036, -0.009] |
| TE | 2.853 | 2.853 | 0.000 (not adopted) | 4.234 -> 4.234 | +0.088 -> +0.088 | n/a |
| All | 4.016 | 4.004 | -0.013 | 5.716 -> 5.699 | +0.046 -> +0.033 | [-0.021, -0.004] |

The same window for each candidate on every position (descriptive; these numbers select nothing):

| Position | A (QB) | B (teammates) | C (A+B) | D (env) |
|---|---|---|---|---|
| QB | -0.154 [-0.284, -0.014] | +0.007 | -0.156 [-0.287, -0.020] | -0.013 |
| RB | +0.001 | -0.014 [-0.034, +0.009] | -0.014 [-0.034, +0.009] | -0.001 |
| WR | +0.003 | -0.024 [-0.037, -0.011] | -0.023 [-0.036, -0.009] | +0.001 |
| TE | +0.003 | -0.010 [-0.021, +0.003] | -0.007 | +0.006 [+0.002, +0.009] |
| All | -0.013 [-0.030, +0.004] | -0.015 [-0.023, -0.007] | -0.030 [-0.048, -0.011] | +0.000 |

**Verdict (rules of section 4).** Pooled over the adopted positions (RB + WR): -0.019 [-0.032, -0.006].

| Check | Result |
|---|---|
| c1 pooled MAE diff < 0 with CI upper < 0 | pass |
| c2 no adopted position significantly worse | pass |
| c3 RMSE | pass |
| c4 bias | pass |
| c5 starters pool (RB 5.437 -> 5.429, WR 6.106 -> 6.086) | pass |
| c6 Start/Sit pairs (65.57% -> 65.73%; close calls 60.52% -> 60.84%, regret 2.878 -> 2.842) | pass |
| **c7 mechanism** | **fail**: RB top target competitor out +0.145, RB1 out +0.122. WR top competitor out -0.023 and changed QB -0.104 / -0.018 pass |

Outcome: **`retain_baseline`.**

**Exploratory 2025-26 replay** (already inspected; 2025-26 injury reports untimestamped, so this is
retrospective):

| | A | B | C | D | composite |
|---|---|---|---|---|---|
| All (n = 8,645) | -0.029 [-0.042, -0.016] | -0.019 [-0.032, -0.004] | -0.048 [-0.063, -0.032] | -0.012 [-0.021, -0.004] | -0.020 [-0.031, -0.007] |
| QB | -0.288 [-0.400, -0.182] | -0.010 | -0.301 [-0.410, -0.193] | -0.098 [-0.152, -0.048] | 0 |
| RB | -0.002 | -0.034 [-0.060, -0.008] | -0.036 [-0.067, -0.005] | -0.004 | -0.036 |
| WR | 0.000 | -0.026 [-0.048, -0.002] | -0.026 [-0.045, -0.007] | -0.003 | -0.026 |
| TE | -0.002 | +0.006 | +0.002 | -0.000 | 0 |

By season: 2025 composite -0.025 [-0.038, -0.012]; 2026 weeks 1-6 0.000 [-0.017, +0.017].

**Context-specific cohorts** (C minus ifp-2 MAE, week-clustered CI; conf = 2023-24, expl = 2025-26):

| Cohort | Rows | n conf / expl | conf | expl | Reading |
|---|---|---|---|---|---|
| Changed expected QB | QB | 232 / 130 | -0.89 [-1.30, -0.49] | -1.24 [-1.82, -0.69] | QB change is the clearest signal |
| Changed expected QB | WR | 996 / 706 | -0.10 [-0.15, -0.07] | -0.06 [-0.09, -0.03] | helps receivers too |
| Changed expected QB | RB | 630 / 419 | -0.02 | -0.09 [-0.16, -0.03] | small |
| Same QB | All | 10,240 / 6,303 | -0.016 | -0.024 | no harm |
| Starting QB injured/absent (backup expected) | QB | 39 / 30 | -1.13 [-1.95, -0.31] | -0.02 | tiny samples |
| Expected QB wrong (evaluation only) | QB | 111 / 50 | +0.69 [+0.17, +1.22] | +1.14 [+0.61, +1.82] | a wrong identification hurts: the feature is only as good as the ID |
| QB uncertain/unknown | QB | 167 / 121 | +0.66 [+0.16, +1.02] | -0.18 | week 1 / unknown handling (below) |
| Top target competitor out | All | 575 / 360 | +0.084 [+0.003, +0.165] (RMSE 6.59 -> 6.50, bias +0.69 -> +0.07) | +0.114 (RMSE 6.15 -> 6.05) | mean improves, MAE worsens |
| Top target competitor returning | WR | 619 / 314 | -0.114 [-0.162, -0.067] | -0.186 [-0.265, -0.096] | reliable |
| RB1 out | RB | 251 / 128 | +0.122 (RMSE 7.22 -> 6.97, bias +1.77 -> +0.42) | -0.167 (RMSE 6.63 -> 6.15) | mean improves, MAE mixed |
| Low volume (< 5 opportunities) | All | 8,566 / 5,598 | -0.039 [-0.061, -0.014] | -0.052 [-0.068, -0.033] | |
| Starters pool | All | 5,616 / 3,588 | -0.022 [-0.042, -0.001] | -0.049 [-0.072, -0.024] | |
| Changed NFL team | All | 2,957 / 1,796 | -0.050 [-0.092, -0.007] | -0.093 [-0.135, -0.044] | |
| Early season / < 3 games | All | 3,292 / 3,211 | -0.016 | -0.033 [-0.061, -0.006] | |

Coverage and abstention are identical by construction: every model forecasts the same 97.7% of labeled
rows, and abstains on players with no game in the last five. All numbers are conditional on appearing.
The unconditional `p_play x points` was not recomputed because the participation model is unchanged.

**Diagnosed failure mode (not fixed here, so the confirmation stays clean).**

* When the expected QB is unknown (mostly week 1), `qb_is_expected` is imputed with the training mean.
  That makes a backup look like a likely starter. Example: a 2023 week-1 backup goes from ifp-2's 1.8
  points to 13.6, and he scored 0. This explains the QB-row `uncertain/unknown` regression above.
* A week-1 expected starter (the previous season's starter if he is still on the team's latest roster)
  must be pre-registered and evaluated prospectively. It cannot be retro-fitted into this confirmation.

## 8. Prospective candidate `ifp-ctx-p1` (pre-registered after the verdict)

`context_challenger.PROSPECTIVE_SPEC`: QB A, RB C, WR C, TE ifp2. `PROSPECTIVE_RULES` uses the existing
`challenger.SHADOW_GATE` thresholds. Per adopted position it requires:

* at least 8 fully final prospective weeks and at least 400 matched appearances;
* a CI upper bound below 0;
* bias within 0.15 and starters-pool MAE not worse.

RMSE and the cohorts are reported, not gating.

* `context_research.py predict --week W` fits ifp2 and the candidate in the same run.
  * It writes a write-once artifact (ridge coefficients as JSON) and a write-once pregame ledger.
  * Teams that have kicked off are excluded.
  * The ledger records hashes of the injury report, rosters, panel, starters and QB stats.
  * It **refuses to run while the week's injury report is not ingested**: a frozen ledger would otherwise
    hold degraded forecasts. On 2026-10-16 the week-7 report was not yet in the live database, so no real
    ledger was written. The command was verified end to end into a scratch directory.
* `context_research.py prospective` scores ledgers of fully final weeks on identical rows. The status
  stays `insufficient_data` until the gate's sample minimums are met.
* Nothing reads the ledger. No registry, production table, frozen production ledger or consumer changes.

## 9. Explanation metadata (Phase 8, research only)

`context_challenger.explain_change` returns, per player-week, the shape the ledger stores for later Player,
Start/Sit, Waivers, Trades and video use:

```json
{"model": "ifp-ctx-p1", "status": "research_only",
 "baseline_points": 11.8, "challenger_points": 10.6, "difference": -1.2,
 "factors": [{"group": "qb", "label": "QB context", "points_vs_training_average": -1.4,
              "inputs": {"qb_epa_delta": -0.21, "qb_cpoe_delta": -3.9, "qb_changed": 1}},
             {"group": "tm", "label": "Teammate context", "points_vs_training_average": 0.6,
              "inputs": {"tm_tgt_gain_pg": 1.3, "tm_tgt_vacated": 0.31, "tm_tgt_returning": 0.0}}],
 "qb_context": {"expected_qb_id": "...", "qb_confidence": "uncertain", "qb_reason": "previous_starter_out",
                "exp_qb_dropbacks": 16.0},
 "teammate_context": {"top_tgt_comp_out": 1, "top_tgt_comp_return": 0},
 "uncertainty": {"qb_confidence": "uncertain", "report_used": 1, "qb_sample_dropbacks": 16.0},
 "caveats": ["... fitted associations relative to the training-average player, not proof that a specific
              teammate or QB caused a number of points.", "Conditional on the player appearing ..."]}
```

These numbers are illustrative only. The UI copy should name the evidence ("expected starter: backup, 16
career dropbacks, evidence: uncertain"; "top target competitor ruled out") rather than attribute points
causally. Nothing is wired into the frontend.

## 10. Evidence quality

| Evidence | Windows | Pregame verifiable? |
|---|---|---|
| Historical replay, development | 2021-22 | injury reports timestamp-filtered; starters and stats from past games only |
| Historical replay, confirmation | 2023-24 | the same; reused window (earlier studies inspected it for other features) |
| Historical replay, exploratory | 2025-26 | **retrospective**: injury reports carry no `date_modified` |
| Prospective | 2026 week 7 onward | write-once ledger; no rows yet |

The QB result replicates in three windows, but every window is historical replay. The teammate result
fails its own mechanism check. Neither is promotion evidence.

## 11. Recommendation and next work

**Recommendation: retain ifp-2 in production and run `ifp-ctx-p1` as a prospective research shadow**
(weekly `predict` before kickoff, after `sync_nfl.py`). Propose a promotion review for the QB position
only if the prospective gate is met.

Next, by measured weakness:

1. **Week-1 / unknown expected QB**: previous-season starter still on the latest roster, pre-registered.
   Biggest identified QB error source.
2. **Calibrated two-QB scenarios**: for the `uncertain` class (56% start rate), mix the two QB profiles
   with a probability fitted on the evidence classes (a logistic model on dev seasons). Do not hand-set it.
3. **Mean-oriented gate for redistribution features**: teammate-out effects improve RMSE and bias but not
   MAE. `expected_points` needs a mean, so a future pre-registration should gate those cohorts on RMSE /
   calibration as well as MAE.
4. **Returning-teammate signal on its own**: the most consistent teammate effect. Worth testing for TE,
   where B was about flat.
5. **Pregame snapshots of the injury report** (the weekly pipeline's `source_provenance` already does
   this for production). This makes 2026+ teammate features verifiable.
6. **QB-receiver pair features** need a raw play-by-play ingestion (free nflverse `load_pbp`). Only worth
   it if 1-2 leave a QB-change gap for receivers.
7. **Offensive line availability** from snap counts (OL snaps exist in `nfl_snap_counts`): a free,
   untested candidate for QB rows.

## Reproduce

```
cd backend
uv run python ../scripts/context_research.py build
uv run python ../scripts/context_research.py develop     # writes the frozen selection once
uv run python ../scripts/context_research.py evaluate    # confirmation + exploratory, evaluation.json
uv run python ../scripts/context_research.py cases
uv run python ../scripts/context_research.py predict --week W   # weekly, before kickoff, after sync_nfl
uv run python ../scripts/context_research.py prospective
```

Runtime on the local machine: about 30 seconds to build the store plus context (cached by input fingerprint) and under
10 seconds per walk-forward window.

# Adaptive established workload and injury-aware role estimation (ifp-aw, research only)

Date: 2026-10-15. Data: local nflverse history 2019-2025 and live 2026 weeks 1-5, the same feature
store and scoring fingerprint (`e165b59686110c7d`) as the ifp-2 report
(`docs/research/ifp2_weekly_learning_report.md`). Code: `backend/app/analytics/adaptive_workload.py`,
`scripts/adaptive_workload_research.py`, tests `backend/tests/test_adaptive_workload.py`.

**Answer.** No. An exposure-weighted, injury-aware established workload and an adaptive
recent-vs-established blend do **not** improve next-week projections over the frozen ifp-2
challenger. All four pre-registered candidates sit within ±0.003 points of ifp-2's MAE on development
(2021-22) and on the already-inspected 2023-2026 replays. Every candidate is **rejected** under the
pre-registered rules. ifp-1.0.0 and ifp-2.0.0 are untouched, and nothing is added to the weekly
pipeline.

The study has two useful findings:

1. **ifp-2's healthy-games-only `xfpl_eb` is already close to the exposure-correct estimate.**
   Treating an injury-shortened game as a fraction of a game changes the established workload little
   in aggregate. Injury exits are only about 3% of games, and the per-snap rate in those games is
   close to the healthy rate.
2. **Usage regimes are asymmetric.** Two or more consecutive games well below the established role
   persist: blending toward them improves MAE by 0.036-0.039 points (95% CI below zero) in both
   development and diagnostic seasons. Two or more consecutive games well above the role regress:
   the same blend worsens MAE by 0.044-0.048 points (CI above zero). The two effects cancel. A
   decline-only blend is the natural next hypothesis, but it was found by looking at these cohorts.
   Testing it honestly needs a new pre-registration evaluated on prospective weeks only.

## 1. What already exists (Phase 0 audit)

| Feature (forecast_features.py) | Current definition | Works | Inaccurate where |
|---|---|---|---|
| `cur_h_games` | Healthy (not `injury_exit`) games this season with xFP | yes | an exit counts as 0 games, so it carries no information at all |
| `xfpl_eb` | (Σ healthy-game xFP + 3 × `prev_xfpl_pg`) / (`cur_h_games` + 3) | only group with a CI below zero in ifp-2 development | drops exits entirely; keeps `role_low`, `blowout_low` and Week-18 rest at face value |
| `h_xfpl_pg`, `h_carries_pg`, `h_targets_pg`, `h_snap_pct` | last ≤ 5 healthy games | yes | adding or replacing raw usage with them was rejected in ifp-2 (worse after exits) |
| `prev_xfpl_pg` | previous season's healthy-game xFP, shrunk 2 games toward the position mean | helps team changes | treats a team change like any other season |
| `inj_exits_recent`, `last_injury_exit` | `injury_exit` = snap drop ≥ 15 pts + play-by-play injury mention, or a new listing within 2 weeks | play-by-play exits are high precision | report-only exits are noisier (audit below) |
| `last_role_low` | snap drop, no injury evidence, no blowout | strongest ifp-2 gain (-3.5% after role drops) | ambiguous by design: rest, unreported injury or demotion |
| `last_blowout_low` | snap drop in a game decided by ≥ 17 | no | — |
| Week 18 | no special case | — | Week-18 rest is classified `role_low` (the ifp-2 report's Jefferson 2023 example) |

The frozen ifp-2 results reproduce exactly through the new harness, which shares the champion's
rows, scoring and bootstrap:

* Confirmation: C 4.031 → ifp-2 4.016 (n = 13,345).
* Exploratory: 4.068 → 4.050 (n = 8,645).
* Development: 4.216 → 4.202 (n = 13,372).
* Barkley's 2026 week-6 conditional projection: 7.57, the same as the ledger.

## 2. The estimator

The estimator asks one question of every past game: how much does it tell us about the player's
normal coaching role?

**Information classes.** Each pre-cutoff game is classified from `classify_games` plus play-by-play
evidence and the schedule's final regular-season week. Each class gets an exposure fraction f:

| Class | Evidence | f |
|---|---|---|
| normal | no snap drop | 1 |
| role_low | drop, no injury or blowout evidence | 1: a demotion is never discounted |
| injury_exit_pbp | drop + play-by-play injury mention | e |
| injury_exit_report | drop + a new listing after it only (lower confidence) | 1 − ½(1 − e) |
| blowout_low, listed_low | drop explained by game script, or a chronic listing | 1 − ½(1 − e) |
| week18_low | any non-injury drop in the season's final regular-season week | 1 − ½(1 − e) |
| unknown | no snap record | 1 |

Here e = snap share / reference snap share, clipped to [0.1, 1].

**Established role.** The formula is

aw_eb = (k·P + Σ xFP_g) / (k + Σ f_g)

over this season's games that have xFP:

* P is last season's role under the same exposure rules, shrunk toward the position mean (the
  position mean for a new player).
* k = 3 games, halved after a team change in the adaptive configuration.
* A game without xFP contributes nothing. It is never zero-filled, and it is counted in
  `aw_xfpl_missing_games`.

Barkley's week-4 exit (2 carries on 7% of snaps against a reference of 60%) is about 0.12 of a game of
evidence at his per-snap rate. It is neither a full game of 2 carries nor discarded.

**Adaptive regime (`aw_role`).** The blend works as follows:

* Take the most recent ≤ 3 face-value games (normal, role_low, unknown). Exits, blowouts and Week-18
  drops are skipped: they neither extend nor break a streak.
* Compare each with the established role computed without those games.
* If the latest n ≥ 2 all deviate in the same direction by ≥ 25%, aw_role = (1 − r)·aw_eb + r·mean,
  with r = n/(n + 2), so 0.5 or 0.6.
* r is a fixed function of pregame evidence and is never fitted to outcomes.

The two-game minimum was set after inspecting one feature value (Barkley's week-2 input, where a
single 71%-snap game already opened a "regime"), before any outcome was computed.

**Normal role vs expected actual workload.** `aw_role` is the opportunity the player would get in a
game he plays start to finish. It is not his expected workload next week, which also depends on
whether he appears (participation model) and whether he finishes the game. There is deliberately no
P(limited | appears) model: prior exits do not predict another exit (ifp-2 report, section 2).

## 3. Does an injury-shortened game carry information? (audit, outcome side)

The audit covers players with a reference snap share ≥ 40% and a pregame `xfpl_eb` ≥ 4, from 2020 to
2026. Classes are assigned with all reports, so this is the outcome side. "Next" is the next
appearance in the same season. The ratio is that game's xFP over the pregame established workload.

| Pos | Class | n | Back next week | Usage in the game | Next game | Next < 60% | Next ≥ 90% |
|---|---|---|---|---|---|---|---|
| RB | normal | 3,249 | 84% | 1.07 | 1.01 | 16% | 59% |
| RB | injury_exit_pbp | 64 | 28% | 0.45 | 0.88 | 24% | 48% |
| RB | injury_exit_report | 138 | 61% | 0.57 | 0.79 | 30% | 39% |
| RB | role_low | 336 | 86% | 0.58 | 0.74 | 41% | 31% |
| RB | blowout_low | 181 | 84% | 0.63 | 0.86 | 26% | 47% |
| WR | normal | 8,033 | 84% | 0.99 | 0.95 | 23% | 54% |
| WR | injury_exit_pbp | 176 | 29% | 0.59 | 0.90 | 26% | 50% |
| WR | injury_exit_report | 304 | 63% | 0.56 | 0.80 | 34% | 41% |
| WR | role_low | 898 | 82% | 0.49 | 0.61 | 49% | 33% |
| TE | injury_exit_pbp | 63 | 25% | 0.64 | 0.79 | 38% | 35% |
| TE | role_low | 351 | 87% | 0.59 | 0.65 | 45% | 31% |

Week-18 drops are measured at the next season's first appearance: RB 0.83 (n = 42), WR 0.78
(n = 107), TE 0.70 (n = 45).

What the audit shows:

* Play-by-play exits mostly preserve the role: the next game runs at 0.88-0.90 of the established
  workload for RB and WR.
* Report-only exits sit between exits and role cuts. About 30-34% of them are followed by a game
  below 60% of the role, against 16-23% after a normal game.
* That is the explicit false-positive estimate: roughly one report-only "injury exit" in seven to
  nine behaves like a genuine role reduction beyond the base rate (11-14 percentage points). This supports giving them half
  confidence.
* `role_low` games behave like real role changes (41-49% stay below 60%). They are correctly never
  discounted.

The audit describes the classes. The class confidences were fixed before it was run and were not
tuned on it.

## 4. Candidates (pre-registered, `CANDIDATES` and `AW_DECISION_RULES`)

* Every candidate keeps the ifp-2 feature set (C + flags + `xfpl_eb` + previous-season core,
  36-week half-life).
* QB keeps ifp-2's QB model exactly. The harness asserts identical QB predictions.
* Penalties are chosen per candidate and position on 2021-22 only.

| Candidate | Change to RB/WR/TE features |
|---|---|
| AW_E | `xfpl_eb` → `aw_eb` (exposure only: no regime, no team-change factor) |
| AW_B | `xfpl_eb` → `aw_role` (exposure + adaptive regime + team-change factor) |
| AW_BX | `xfpl_eb` → `aw_role`, plus `aw_r` and `aw_eb` (lets ridge weigh the regime) |
| AW_B_noexp | AW_B with every f = 1 (ablation, not selectable) |

## 5. Results

**Pooled MAE, with candidate − ifp-2 and a week-clustered 95% CI.** RB/WR/TE/QB are on identical
rows; the selection rule picked AW_E.

| Window | C | ifp-2 | AW_E | AW_B | AW_BX | AW_B_noexp |
|---|---|---|---|---|---|---|
| Development 2021-22 (n = 13,372) | 4.216 | 4.202 | 4.203 (+0.000 [−0.000, +0.001]) | 4.203 (+0.000 [−0.002, +0.003]) | 4.203 (+0.001 [−0.002, +0.004]) | 4.204 (+0.001 [−0.001, +0.004]) |
| 2023-24, inspected (n = 13,345) | 4.031 | 4.016 | 4.017 (+0.000) | 4.019 (+0.003) | 4.016 (+0.000) | 4.018 (+0.002) |
| 2025-26, inspected (n = 8,645) | 4.068 | 4.050 | 4.049 (−0.001) | 4.053 (+0.003) | 4.048 (−0.002) | 4.053 (+0.003) |
| 2023-26 pooled (n = 21,990) | 4.045 | 4.030 | 4.029 (−0.000 [−0.001, +0.000]) | 4.032 (+0.003 [+0.001, +0.005]) | 4.029 (−0.001 [−0.002, +0.001]) | 4.032 (+0.002 [+0.000, +0.004]) |

RMSE and bias are unchanged to the second decimal (pooled RMSE 5.744-5.746, bias +0.010 to +0.018).

Start/Sit pair accuracy and mean regret on 2023-26 pooled:

* ifp-2: 65.47%, 2.492.
* AW_E: 65.49%, 2.490.
* AW_B: 65.47%, 2.492.
* AW_BX: 65.48%, 2.493.

**By position, 2023-26 pooled (candidate − ifp-2).** All differences are within ±0.004:

* RB: AW_E +0.001, AW_B +0.004 [−0.001, +0.009].
* WR: AW_B +0.003 [+0.000, +0.006].
* TE: AW_BX −0.003 [−0.006, −0.000].

**Required cohorts, 2023-26 pooled.** MAE, with bias in parentheses; [CI] is candidate − ifp-2.

| Cohort | n | ifp-2 | AW_E | AW_B | AW_BX |
|---|---|---|---|---|---|
| RB starters pool | 2,832 | 5.582 | 5.584 | 5.586 | 5.580 |
| RB high volume (healthy xFP ≥ 15) | 291 | 7.114 | 7.117 | 7.095 [−0.041, +0.002] | 7.092 [−0.041, −0.003] |
| Injury return | 778 | 4.317 (−0.03) | 4.318 (−0.04) | 4.322 (−0.03) | 4.308 [−0.019, −0.000] |
| Multiple early exits | 302 | 4.023 (−0.36) | 4.017 | 4.012 | 4.018 |
| Unexplained role reduction | 1,682 | 3.120 (−0.11) | 3.122 | 3.127 [+0.001, +0.012] | 3.123 |
| Breakout regime (pregame streak ≥ +2) | 1,284 | 5.334 (+0.15) | 5.333 | 5.381 (−0.12) [+0.028, +0.063] | 5.342 |
| Decline regime (pregame streak ≤ −2) | 3,239 | 2.948 (−0.10) | 2.949 | 2.912 (+0.03) [−0.043, −0.029] | 2.956 |
| Team changed | 4,753 | 3.585 | 3.587 | 3.588 | 3.580 [−0.009, −0.002] |
| Week 18 target | 1,173 | 4.180 | 4.180 | 4.184 | 4.177 |
| After a Week-18 low game | 175 | 4.133 (+0.79) | 4.132 | 4.152 [+0.004, +0.037] | 4.133 |
| Genuine demotion (outcome-defined) | 1,735 | 3.684 (−3.10) | 3.687 (−3.10) | 3.682 (−3.09) | 3.684 (−3.10) |
| Breakout (outcome-defined) | 677 | 9.049 (+8.48) | 9.049 | 9.039 | 9.063 |
| Limited history (< 3 games) | 1,032 | 3.059 | 3.059 | 3.065 [+0.004, +0.009] | 3.063 |

The development cohorts (2021-22) show the same regime asymmetry for AW_B:

* Breakout regime: +0.044 [+0.018, +0.071].
* Decline regime: −0.039 [−0.047, −0.031].

The injury-return cohort is flat. C → ifp-2 already moved its bias from +0.25 to −0.03, at the cost
of +0.014 MAE, and no candidate moves it further.

## 6. Decision (`AW_DECISION_RULES`)

* **AW_E (selected on development): rejected.** It has no development gain (+0.000, CI
  [−0.000, +0.001]). No diagnostic rule fires.
* **AW_B: rejected.** It has no development gain, and its 2023-26 pooled MAE is worse (+0.003).
* **AW_BX: rejected.** It has no development gain. Its diagnostic numbers are the best of the four
  (−0.001 pooled; TE, team-change and injury-return CIs below zero), but those seasons cannot select
  a model.
* **AW_B_noexp (ablation): rejected.** It is worse, and WR is significantly worse.

How the Phase-5 rejection criteria came out:

* Injury-return MAE: no candidate is materially worse (all within ±0.6%).
* Over-projecting genuine demotions: none (outcome-defined demotion bias within 0.02).
* Manual player rules: none exist.
* RMSE and bias: unchanged.
* Pregame-only data: yes. It is enforced by `role_features_for_week`'s cutoff and tested.
* Low-sample stability: AW_B and AW_BX are slightly worse on limited history (+0.006 and +0.004,
  under the 2% threshold).
* Retrospective injury data: labelled. The 2025-2026 injury rows have no `date_modified`, so their
  pre-kickoff filter cannot be applied. Play-by-play exits describe completed games only.

The research config `ifp-aw-0.1.0` is written with `status: "rejected"` to
`<model dir>/research/adaptive_workload/` (git-ignored), next to the report.

## 7. Case studies (real data; explanations are inferred)

### Saquon Barkley, 2026 week-6 pregame forecast

| Week | Carries | Targets | Snap share | Reference | xFP | Class | f |
|---|---|---|---|---|---|---|---|
| 1 | 15 | 2 | 71% | 74% | 11.1 | normal | 1.00 |
| 2 | 4 | 2 | 16% | 71% | 4.8 | injury_exit_pbp | 0.22 |
| 3 | 15 | 2 | 72% | 61% | 15.1 | normal | 1.00 |
| 4 | 2 | 1 | 7% | 60% | 2.3 | injury_exit_pbp | 0.12 |
| 5 | Out (hamstring); week-6 report: did not practice | | | | | | |

Workload estimates, in expected league points per game unless noted:

* Raw recent: 11.0 carries per game, `xfp_recent` 9.0, `snap_trend` −0.53.
* Healthy: 18.0 carries per game.
* ifp-2's `xfpl_eb`: 13.77, from 2 healthy games plus last season's 14.20.
* Experimental `aw_eb` (= `aw_role`, no regime): 14.77, from 2.34 games of evidence plus last
  season's exposure-weighted 15.20.
* Ablation without exposure (exits counted as full games): 12.48.

Conditional projection if he plays:

* ifp-2: 7.57.
* AW_E: 7.90.
* AW_B: 7.78.
* AW_BX: 7.97.
* AW_B_noexp: 7.14.

The experimental workload restores about 1 more point of established xFP and about 0.3 projected
points. That is still far below his healthy-game level. The main drag is `snap_trend` and the
last-5 production features, not the workload estimate. This is one player-week and proves nothing
about aggregate accuracy, which is unchanged (section 5).

### Contrasts, selected by data queries over 2023-26 (no player rules)

These cohorts are selected with the target week's own usage, so they flatter whichever estimator
reacts in that direction. The pregame regime cohorts in section 5 are the fair comparison.

* **Lost a starting role: Diontae Johnson, 2024 week 12 (WR).**
  * Three consecutive face-value games well below his role (streak −3, last game `role_low`).
  * `xfpl_eb` 12.2 and `aw_eb` 12.4, but `aw_role` 7.7.
  * Projections: ifp-2 6.32, AW_E 6.27, AW_B 5.47. Actual 0.0, on 1.6 xFP.
  * In the 58 such rows, AW_B's MAE is 6.10 against ifp-2's 6.39.
* **Broke out: Tank Dell, 2023 week 11 (WR).**
  * Two consecutive games far above his role (streak +2): `aw_role` 19.0 against `xfpl_eb` 12.4.
  * Projections: ifp-2 12.78, AW_B 13.68. Actual 28.9.
  * In the selected 274 rows AW_B looks better (7.53 vs 7.70). Over all 1,284 pregame
    breakout-regime rows it is worse (5.381 vs 5.334): most positive streaks regress, and the ones
    selected here are the exceptions.
* **Ambiguous injury classification: Kenneth Walker III, 2023 week 10 (RB).**
  * His last game is a report-only `injury_exit_report`: a snap drop followed by a new listing, with
    no play-by-play injury mention.
  * Projections: ifp-2 14.30, AW_E 14.23, AW_B 14.93 (C 12.69). Actual 19.7, on 14.5 xFP: the role
    was intact.
  * Ladd McConkey, 2026 week 3 (`listed_low`, chronic listing) is the opposite kind of ambiguity:
    `aw_eb` 12.4 against `xfpl_eb` 9.2. The experimental estimate keeps more of the drop game, and
    he produced 9.2 xFP.

## 8. What changes, and what does not

Research only:

* `adaptive_workload.py`, its script, its tests and this report.
* Two isolation-test allow-lists now name the new module. A new test asserts that nothing in
  `app/` imports it.

Unchanged:

* ifp-1.0.0 and its artifacts; `challenger_config.json` (hash checked in tests).
* `weekly_learning`, every ledger, the ESPN/Ours switch, Start/Sit, lineups, the API, schedules
  and dependencies.

Reproduce (about 3 minutes on 4 workers):

```
cd backend
uv run python ../scripts/weekly_learning.py sync-exits --history-seasons 2019-2025   # once
uv run python ../scripts/adaptive_workload_research.py run
uv run python ../scripts/adaptive_workload_research.py audit
uv run python ../scripts/adaptive_workload_research.py cases
```

## 9. Limitations and next step

* 2023-2026 were inspected by earlier work. Here they can only reject a candidate, never select one.
* The class confidences (½), the regime threshold (25%), its weight (n/(n+2)) and the team-change
  factor (½) were set a priori and not tuned. A tuned version would need data no one has looked at
  yet.
* Exposure is measured as a snap share relative to the player's own reference. Play-by-play
  last-touch timing is available but unused.
* **Next step (not done here):** pre-register a decline-only regime (blend toward persistent
  declines, never toward streaks above the role). Evaluate it only on prospective frozen weeks
  (2026 week 7 onward) through the weekly pipeline's shadow gate, after explicit approval to add a
  third shadow model.

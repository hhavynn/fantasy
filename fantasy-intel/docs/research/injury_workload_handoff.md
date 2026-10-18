# Injury timeline and workload handoff (ifp-iwh, research only)

**Question.** Why do our projections undervalue a starter coming back from injury and barely move a backup
whose starter is ruled out? And does an explicit, team-level model of who inherits the vacated work, and who
takes it back on a return, make ifp-2's conditional projections more accurate?

**Short answer.**

* **Root cause.** The conditional model never sees teammate availability: a backup's projection moves only
  through his own last five games, and a starter's injury-shortened games stay in his raw averages. Separately,
  a run made for a week *beyond* the next unplayed week applied that earlier game's injury status to the later
  week (a real bug, fixed), and the production run is frozen mid-week, before Friday's final designations.
* **The handoff challenger is the most accurate candidate this project has tested, but it is not promoted.**
  Adding two handoff features to ifp-2's RB/WR/TE ridges:
  * Confirmation 2023-24 (run once, adopted positions pooled, n = 12,015): MSE -0.377 [-0.517, -0.239], MAE
    **-0.039 [-0.048, -0.031]** (about 1%; earlier candidates were -0.005 to -0.019), Start/Sit pair accuracy
    66.14% vs 65.89%. Every position improves on MSE and MAE.
  * Exploratory 2025-26: MSE -0.570 [-0.733, -0.393], MAE -0.040 [-0.052, -0.028].
  * The teammate-out mechanism holds: RB rows with the lead back out go from bias +1.07 to +0.44 and RMSE 5.68
    to 5.45; backups whose lead returns improve by 0.25 MAE.
  * It **fails** the pre-registered mechanism check on the returning player himself (MSE +0.04, bias +0.34 ->
    +0.50, n = 185). Diagnosed (section 7.3): the participation factor treats a Questionable-and-inactive game
    as a role absence. **Verdict by the frozen rules: `retain_baseline`.** The fix, **ifp-iwh-0.1.1**, is
    implemented and judged only on write-once pregame ledgers from 2026 week 7 (section 10); no historical
    window is re-run.
* **Rest of season is badly calibrated for injured players.** For players Out/Doubtful at the cutoff, the
  current ROS path's P(plays next week) averages **0.91; they actually played 39% of the time** (n = 409,
  2023-24; Brier 0.514). A simple empirical return-rate table fitted on 2019-22 averages 0.34 (Brier 0.239).
  Not changed in production here (it changes numbers); proposed as a gated follow-up.

Code:

* `backend/app/analytics/injury_workload.py`: week-specific injury timeline, established healthy role,
  workload handoff, empirical return rates, explanation metadata.
* `backend/app/analytics/injury_challenger.py`: the challenger, pre-registered rules, cohorts.
* `scripts/injury_workload_research.py`: `build`, `develop`, `evaluate`, `ros`, `cases`, and for the prospective
  shadow of ifp-iwh-0.1.1, `predict --week W` and `prospective` (section 10). `develop` and `evaluate` refuse to
  run under 0.1.1.
* Tests: `backend/tests/test_injury_workload.py` (the mandatory Week 6 / Week 7 scenarios plus edge cases).

Nothing in the app imports these modules (tested). ifp-1.0.0, ifp-2.0.0, the model registry, approved
artifacts, production runs and their hashes, the frozen ledgers, the ESPN/Ours switch, Start/Sit, trades and
waivers are untouched. The one production code change is a correctness guard (section 2, bug 1).

## 1. Audit of the pipeline (before any change)

Data flow: nflverse injury reports (`nfl_injuries`) and weekly rosters (`nfl_rosters_weekly`) ->
`forecast_features.load_injuries` / `load_rosters` -> `features_for_week` (last 5 games played, current +
previous season, healthy-game classes) -> ifp-2 ridge per position (`challenger.fit_linear`, conditional on
appearing) -> `participation.py` p_play (week-W report) -> `independent_projection_store.build_live`
(withholding, ESPN fallback, rest-of-season horizon) -> `independent_projection_runs/rows` ->
`projection_source` / `forward_ours` -> frontend.

What the real 2026 data shows for the three investigation players (nflverse, synced through week 6 reports):

| Player | Facts | Stored ifp-2 week-6 row (run of 2026-10-16 04:46 UTC) |
|---|---|---|
| Saquon Barkley (PHI) | Weeks 1-4 played; weeks 2 and 4 were injury exits (16% and 7% snaps). Out week 5 (hamstring), **Out week 6** on the week-6 report. | conditional 7.57, p_play 0.29, expected 2.19, flag "ESPN injury status QUESTIONABLE" |
| Dameon Pierce (PHI) | Practice squad weeks 1-4, promoted week 5, 77% snaps in week 5. Tank Bigsby on IR from week 5, Will Shipley on IR from week 6. | conditional 7.73, p_play 0.96, flag "part-time role (recent snap share 22%)" |
| Breece Hall (NYJ) | Weeks 1-3 played, Out weeks 4-6 (quadriceps). Braelon Allen took over (94% / 86% snaps). | week 6: conditional 12.50, p_play 0.002. A stored **ifp-1.0.0 week-7** horizon-0 run: conditional 13.60, p_play 0.04 |

So scenario A (Saquon Out, Pierce as the replacement) is the real week 6. Scenario B (Saquon back in week 7)
and the week-7 Breece return cannot be verified: the week-7 report does not exist yet, so they are tested
with synthetic fixtures only.

The ten questions from the brief:

| # | Question | Finding |
|---|---|---|
| 1 | Missed games counted as zero-production games? | **No.** The panel holds appearances only; a missed week is not a row. `pts_season` etc. average games played. |
| 2 | Long absences decay healthy history too fast? | **No time decay.** The window is the last 5 *games played* in the current + previous season (Breece's window is weeks 1-3 plus two 2025 games). A player out for more than a full season abstains. |
| 3 | Returning players lose role priors? | **Partly.** Injury-shortened appearances stay in the raw last-5 averages (`carries_pg`, `snap_pct`, `pts_recent`, `xfp_recent`). For Saquon two exits put last-5 carries at 11.0 vs 18.0 healthy; `snap_trend` alone costs him 3.2 points. ifp-2's `xfpl_eb` and flags restore part of it (documented in `ifp2_weekly_learning_report.md`, which also found that restoring the full healthy workload overshoots: returners get ~85% of their prior opportunities). |
| 4 | Backups get too little when a starter is out? | **Confirmed limitation.** The conditional model has no teammate-availability input at all; a backup moves only through his own recent games. Pierce's window mixes one PHI start with 2025 backup games (22% snaps). |
| 5 | Backups keep an inflated role after the starter returns? | **Confirmed limitation**, same cause: Braelon Allen's two bell-cow games stay in his last-5 average whether or not Hall returns. |
| 6 | Current designations propagated across future weeks? | **Confirmed bug (fixed, section 2).** `build_live` horizon-0 runs for a week beyond the next unplayed week apply that earlier game's ESPN status (and the ESPN designation fallback) to the later week. The stored ifp-1 week-7 run was made while week 6 was unplayed: Breece's week-6 "DOUBTFUL" became his week-7 p_play of 0.04. Rest-of-season runs (`projected_from_week`) already handled this correctly. |
| 7 | ROS reuses stale injury and role assumptions? | **Confirmed limitation (by design, labeled experimental).** Features are frozen at the cutoff week for every target week: a temporary starter keeps his role all season, and a player Out at the cutoff gets an *undesignated* p_play for every later week. Section 6 measures the second effect. |
| 8 | Participation suppresses the conditional projection? | **No.** `projected_points` is conditional; p_play multiplies only `expected_points`. |
| 9 | Fantasy IR confused with NFL IR? | **No.** `player_availability` keeps ESPN status, NFL reserve, fantasy IR slot and eligibility separate; build_live only flags the fantasy IR slot (`test_ir_semantics.py`). |
| 10 | Cached forecasts or stale sync? | **Confirmed operational gap (not changed here).** The learning cycle freezes the production run Tue-Thu before the first kickoff. Friday's final game designations never reach that run: Saquon's stored week-6 row used ESPN's mid-week QUESTIONABLE (p_play 0.29) although the nflverse week-6 report now says Out. A later re-projection for teams still pregame is a follow-up (section 9). |

## 2. Correctness fixes (shipped)

1. **Horizon guard in `build_live`** (`independent_projection_store.check_horizon`). A run's cutoff week must
   be the next unplayed week: the earliest week with an outstanding game over the whole slate (a final
   Thursday game does not end its week; an unscored game counts as postponed or cancelled only when a game
   with a strictly later kickoff is final). A horizon-0 run for a later week now raises `HorizonError` and
   says to project it as a rest-of-season target (`projected_from_week`). Valid runs are byte-identical; the
   learning cycle always passes the right week. Regression tests in `test_ros_projections.py`:
   `test_a_horizon_zero_run_beyond_the_next_unplayed_week_is_refused` and
   `test_the_next_unplayed_week_is_judged_over_the_whole_slate`.
   Three existing ROS tests that simulated a later cutoff with the same unplayed sources now mark the earlier
   weeks final first (`played_through`).

Nothing else in production changes. The existing stored week-7 ifp-1 run is immutable and stays; it is no
longer reproducible by mistake.

## 3. Injury timeline (`injury_timeline`)

One row per QB/RB/WR/TE with a lookback appearance or a roster row before the cutoff week W, for a target week
T >= W. Facts only:

| Column | Meaning |
|---|---|
| `designation` | week-specific: `out`, `doubtful`, `questionable`, `listed` (practice report, no game status), `not_listed` (report published, player absent), `no_report`, `future_unknown` (T > W) |
| `report_week`, `report_status`, `practice_status`, `injury` | the cutoff week's report row, labeled with its week |
| `roster_status`, `roster_week`, `nfl_reserve` | latest nflverse weekly roster row before W |
| `reserve_earliest_week` | the minimum-games rule of `player_availability.derive`, only when the GSIS reserve code is known (`load_roster_codes`); never a guessed return date |
| `last_app_*`, `last_healthy_*`, `last_game_injury_exit` | from the panel and `classify_games` |
| `missed_team_games`, `missed_injured` | his current team's final games since his last appearance; of those, games he was Out/Doubtful or on reserve |
| `return_evidence` | after missed games: `listed_without_designation`, `questionable`, `activated_from_reserve` or `none` |
| `status_class`, `status_reason` | `available`, `available_returning`, `uncertain`, `unavailable`, `unknown`; an ordinal evidence label, never a probability |
| `confidence` | `report`, `roster_only`, `none` |

Rules that make it week-specific: an Out designation describes its own week's game. For T > W nothing
describes week T, so a player injured at the cutoff is `uncertain` ("return neither assumed nor ruled out"),
unless the cutoff report already shows him back (then `available`) or a reserve rule dates his eligibility
after T (then `unavailable`). Missed games without any injury listing are a role or roster decision, not an
injury. Retrieval timestamps stay in `injury_freshness.py` (`injury_retrievals`): the timeline is built from
`Sources`, which drops nflverse rows modified at or after kickoff; 2025+ rows have no modification time and are
retrospective.

## 4. Established healthy role (`established_roles`)

Per player, over his last <= 8 **healthy** games in the lookback (current + previous season): carry share of
team rushes, target share of team targets, goal-line share, snap share, xFP per game. A missed game is not a
row and an injury-exit game is not healthy, so neither lowers the role. Shares shrink toward the **newcomer
prior** (mean share in a player's first game for a new team from week 2 on, measured from the same pre-cutoff
data) by one game, so a one-game sample does not become a starter's claim.

A genuine role change is not undone: a healthy demotion stays in the healthy window, and missed games without
an injury listing lower the player's **participation** factor (share of the team's last 4 games he played or
missed injured).

## 5. Workload handoff (`workload_handoff`)

For each team and target week:

    expected carries = team rush attempts per game x RB group share of rushes x player's expected share
    expected targets = team targets per game x position group share of targets x player's expected share

* Team volume: this season's per-game rate shrunk toward last season's team rate, itself shrunk toward the
  league (3 games each). Group share (RB carries; RB / WR / TE targets): this season shrunk toward the league
  by 4 games. Carries and targets are separate pools; goal-line share follows the carry reallocation.
* Claim = established share x participation. The pool is the available players (Out, Doubtful, reserve,
  inactive and departed players leave it; Questionable players stay at horizon 0).
* Expected share: proportional to claims, then shrunk (2 games) toward what each player actually received in
  the team's lookback games that **look like this week's room**: every other material present player played
  and none of the key absentees did (the largest absent claims covering 70% of the absent claims). For a
  backup that is his share in the games his lead missed; for a returning starter's backup it is his share
  once the starter played again. Shares are renormalized to the group's share, so the room's budget always
  adds up.
* Nothing gets 100% mechanically: a committee splits the vacated work; a lone qualified backup can take most
  of it.
* Expected league xFP = player multiplier x (a x expected carries + b x expected targets), with a, b fitted per
  position by least squares on pre-cutoff healthy games and the multiplier shrunk toward 1 by 3 games.
* Horizon >= 1: a teammate who is `uncertain` is enumerated as present/absent scenarios weighted by the
  empirical return rate (`fit_return_rates`, section 6). Without a fitted table he stays out and every row
  says `ho_scenarios = 0` (labeled status quo).
* Every row is conditional on **its** player appearing (an Out lead back still gets a conditional share; his
  p_play is the participation model's job).

The challenger gets two features per RB/WR/TE row: `ho_exp_xfpl` and `ho_shift_pg` (expected opportunities
minus the raw last-5 average, the correction ifp-2 cannot see). QB rows keep ifp-2.

## 6. Rest of season

Production ROS runs (`build_live(..., projected_from_week=W)`) compute every feature at the cutoff week W and
give week T an **undesignated** p_play. Two consequences:

1. **A player injured at the cutoff is projected as healthy for every later week.** Measured on 2023-24
   (players designated Out/Doubtful for week W whose team plays in W+1; `ros` command):

   | Group | n | Actually played W+1 | Current ROS p_play | Return-rate table | Brier current / table |
   |---|---|---|---|---|---|
   | All | 409 | 0.391 | 0.910 | 0.340 | 0.514 / 0.239 |
   | Missed 0 games before W | 223 | 0.363 | 0.958 | 0.318 | 0.584 / 0.232 |
   | Missed 1 | 129 | 0.411 | 0.868 | 0.373 | 0.464 / 0.243 |
   | Missed 2-3 | 55 | 0.473 | 0.824 | 0.354 | 0.350 / 0.268 |
   | RB / WR / TE | 124 / 180 / 105 | 0.355 / 0.422 / 0.381 | 0.89 / 0.91 / 0.94 | 0.31 / 0.36 / 0.35 | |

   The table (`fit_return_rates`): P(appears in the team's h-th game after W | Out/Doubtful at W), by position
   and games already missed, fitted only on episodes whose outcome game precedes the forecast. Fitted on
   2019-22 and checked on 2023-24: h = 1 0.335 vs 0.343, h = 2 0.513 vs 0.560, h = 3 0.584 vs 0.667, h = 4
   0.612 vs 0.711 (later horizons drift upward; a refit per season is needed). No recovery date is ever
   derived from games missed.

   Live example (cutoff week 6): the current ROS path gives Saquon Barkley p_play 0.93 and Breece Hall 0.96
   for week 7, both Out for week 6; the table gives 0.39 and 0.40.
2. **A temporary starter keeps his role all season.** The handoff's horizon mode enumerates the uncertain
   teammate's return as weighted scenarios. Dameon Pierce's week-7 expected share falls from 70% (status quo,
   Saquon still out) to 45% when Saquon's 0.39 return probability is mixed in; Braelon Allen's from 71% to 50%.

Neither is wired into production: both change numbers (governance category B).

## 7. Results

### 7.1 Protocol

Pre-registered in `injury_challenger.DECISION_RULES` and committed (f381498) before the first development
run: development 2021-22 selects positions (MSE CI upper < 0, MAE not worse by > 0.005, bias within 0.15);
confirmation 2023-24 runs once with seven checks; 2025-26 is an exploratory replay (untimestamped injury
reports). Same store, same weeks and same training rows as the ifp-ctx study; ifp-2's frozen penalties and
recency weights; no hyperparameter searched. All numbers are **conditional on appearing**.

### 7.2 Results

Development: RB, WR and TE all adopted (MSE -0.570 / -0.288 / -0.101, MAE -0.063 / -0.032 / -0.023, all CIs
below 0).

**Confirmation 2023-24 (frozen adoption, identical rows):**

| Position | n | MAE base -> iwh | RMSE base -> iwh | Bias base -> iwh | MSE diff [95% CI] |
|---|---|---|---|---|---|
| RB | 3,398 | 4.006 -> 3.963 | 5.676 -> 5.636 | -0.02 -> -0.03 | -0.457 [-0.752, -0.108] |
| WR | 5,409 | 4.211 -> 4.166 | 5.961 -> 5.925 | +0.03 -> +0.08 | -0.431 [-0.649, -0.202] |
| TE | 3,208 | 2.853 -> 2.826 | 4.234 -> 4.210 | +0.09 -> +0.10 | -0.200 [-0.287, -0.115] |
| All (QB unchanged) | 13,345 | 4.016 -> 3.981 | 5.716 -> 5.686 | +0.05 -> +0.07 | -0.339 [-0.465, -0.215] |

Pooled MAE over adopted positions -0.039 [-0.048, -0.031]. Starters pool RB 5.437 -> 5.411, WR 6.106 -> 6.063,
TE 4.898 -> 4.879. Start/Sit pairs 66.14% vs 65.89%, regret 2.42 vs 2.45.

| Check | Result |
|---|---|
| c1 pooled MSE CI upper < 0 | pass |
| c2 pooled MAE <= +0.010 | pass (-0.039) |
| c3 no position significantly worse | pass |
| c4 bias | pass |
| c5 starters pool | pass |
| c6 Start/Sit pairs | pass |
| **c7 mechanism** | **fail**: lead RB out (n 357) MSE -2.65, bias +1.07 -> +0.44 pass; WR1 out (561) -1.43 pass; TE1 out (340) -0.90 pass; **returning player himself (185) MSE +0.04, bias +0.34 -> +0.50 fail** |

**Outcome: `retain_baseline`.** Exploratory 2025-26 (n = 8,645): MAE 4.050 -> 4.014, MSE -0.515
[-0.662, -0.355], pairs 65.9% vs 65.3%; c7 fails there on TE1-out instead (n 145, MSE +0.25) while the
returning cohort passes (MSE -1.09).

**Cohorts, confirmation (all positions; MAE and RMSE base -> iwh, week-clustered 95% CI of the diff, bias;
share MAE = mean absolute error of the week's actual share, raw last-5 share -> handoff share):**

| Cohort | n | MAE | MAE diff CI | RMSE | MSE diff CI | Bias | Share MAE |
|---|---|---|---|---|---|---|---|
| RB, lead back out (teammates) | 357 | 3.947 -> 3.879 | [-0.20, +0.07] | 5.68 -> 5.45 | [-4.68, -0.96] | +1.07 -> +0.44 | carries 0.146 -> 0.121 |
| RB backup takes over (>= 50% share) | 42 | 6.219 -> 6.007 | [-1.16, +0.78] | 7.94 -> 7.12 | [-26.5, +0.9] | +3.53 -> +0.90 | carries 0.289 -> 0.166 |
| RB committee after lead out | 151 | 4.261 -> 4.191 | [-0.22, +0.08] | 5.69 -> 5.50 | [-4.02, -0.03] | +1.11 -> +0.38 | carries 0.170 -> 0.146 |
| RB backup after lead returns | 41 | 3.354 -> 3.101 | [-0.45, -0.05] | 4.45 -> 4.23 | [-4.17, -0.17] | -1.14 -> -0.67 | carries 0.111 -> 0.092 |
| Lead RB returning after 1 game | 3 | 4.37 -> 4.13 | n/a | | | | |
| Lead RB returning after 2+ games | 13 | 5.09 -> 5.07 | n/a | 6.93 -> 7.04 | | -0.24 -> +0.50 | |
| Returning, Questionable or limited | 9 | 4.21 -> 4.25 | n/a | | | | |
| WR1 out (teammates) | 561 | 3.807 -> 3.770 | [-0.10, +0.03] | 5.67 -> 5.54 | [-2.30, -0.69] | +0.67 -> +0.38 | targets 0.057 -> 0.053 |
| WR after WR1 returns | 72 | 3.083 -> 2.879 | [-0.33, -0.12] | 3.84 -> 3.68 | [-2.41, -0.26] | -1.43 -> -1.10 | targets 0.041 -> 0.037 |
| TE1 out (teammates) | 340 | 2.465 -> 2.427 | [-0.08, +0.00] | 3.68 -> 3.56 | [-1.24, -0.49] | +0.72 -> +0.52 | targets 0.038 -> 0.041 |
| TE after TE1 returns | 20 | 2.172 -> 1.978 | n/a | 2.85 -> 2.63 | | -2.12 -> -1.90 | |
| Returning player himself | 204 | 4.009 -> 4.002 | [-0.10, +0.09] | 6.10 -> 6.10 | [-1.17, +1.33] | +0.53 -> +0.68 | |
| Long absence (4+ missed) | 33 | 3.810 -> 3.881 | [-0.21, +0.35] | 5.99 -> 6.04 | [-1.26, +2.46] | +0.75 -> +1.14 | |
| Changed team during absence | 207 | 3.171 -> 3.123 | [-0.14, +0.03] | 4.77 -> 4.72 | [-1.22, +0.18] | -1.06 -> -0.96 | |
| No availability change | 1,916 | 5.364 -> 5.363 | [-0.01, +0.00] | 6.97 -> 6.97 | [-0.07, +0.03] | -0.05 -> -0.06 | |
| Starters pool | 5,616 | 5.744 -> 5.716 | [-0.045, -0.008] | 7.38 -> 7.36 | [-0.63, -0.08] | +0.28 -> +0.30 | |

Reading it:

* **Teammate out: the mean is fixed, MAE moves less.** The earlier redistribution study saw MAE *worsen* on
  these cohorts; here it improves slightly (CIs straddle 0) while RMSE and bias improve clearly. The
  room-matched evidence (how the team actually split the work without that player) is what changed: the
  carry-share error of the backups falls from 0.146 to 0.121, and from 0.289 to 0.166 for backups who take
  over.
* **Returns reverse the temporary role**: backups and WR2s whose lead comes back improve by 0.20-0.25 MAE.
* **The returning player himself is not helped** (next section), and the lead-back return cohorts are tiny
  (3 and 13 rows): the per-player return effect is not measurable with historical data at this sample size.
* Most of the pooled gain is outside the narrow cohorts: `ho_exp_xfpl` is also a better established-role
  measure for players whose room changed weeks ago (long-term absences, departures), which ifp-2's own
  window catches only slowly.

**Against the earlier teammate candidates on the same store and weeks** (confirmation 2023-24, all positions,
MAE minus ifp-2): ifp-ctx B (teammate budget, `qb_teammate_context.md`) -0.015; ifp-ret (returning teammates only,
`returning_teammate_features.md`) about 0 (-0.005); **ifp-iwh -0.036**. ifp-ret's RB-when-RB1-returns gain (-0.37)
is larger than ifp-iwh's on the comparable cohort (-0.25), so the two are complementary rather than redundant.

### 7.3 Why the returning-player check failed (diagnosed after the verdict; changes nothing above)

Every number in sections 7.1-7.2 and 7.4 is **ifp-iwh-0.1.0** (handoff iwh-0.1.0), reproducible at commit
d6fed66. The fix below shipped later as iwh-0.1.1 and is evaluated only prospectively (section 10).


On the confirmation returning cohort the candidate *lowers* projections (mean -0.18), concentrated in rows
whose participation factor is below 1 (part 0.5: mean -0.39; part 0.8: -0.19; part 1.0: -0.03). Those players
missed games while listed **Questionable** (or on the practice report without a game status) and then
inactive: the participation factor counts only Out/Doubtful/reserve weeks as injury absences, so it read those
games as role absences and cut the claim. Pre-registered fix for **iwh-0.1.1** (left out of PR #103 to keep the
evaluated code and the results identical; implemented since): a missed game counts as an injury absence when the
player was on that week's report with any injury listing. It can only be judged on prospective weeks.

### 7.4 Unconditional expected points

Not recomputed. The participation model is unchanged, so `expected_points = p_play x conditional` moves only
through the conditional estimate; a walk-forward participation replay on the store's roster population is
follow-up work (section 9). The ROS p_play finding in section 6 is the larger unconditional error.

## 8. What is live, what is research

| Change | Status |
|---|---|
| Horizon guard in `build_live` (no horizon-0 run past the next unplayed week) | **live** (correctness) |
| Injury timeline, established role, workload handoff, return-rate table, explanation metadata | research module, not imported by the app |
| ifp-iwh-0.1.0 challenger | `retain_baseline` (failed c7); not promoted, no registry change, ifp-2.0.0 unchanged |
| ifp-iwh-0.1.1 (section 7.3 fix) | research; prospective pregame shadow ledger from 2026 week 7 (section 10) |
| ROS p_play from the empirical return table; ROS teammate-return scenarios | evidence only; needs a gated proposal and owner review |
| Re-projection after Friday's final reports | not changed; follow-up |

**Real 2026 cases** (`cases` command; "old" = the stored production ifp-2 row; "refit" = ifp-2 refitted on the
same rows, which reproduces the stored conditional exactly; "new" = ifp-iwh; p_play now = the participation
model with the week-6 report as synced today):

| Player | Week | Injury / role status | Old conditional | New conditional | Old expected | New expected (p_play now) | Change from |
|---|---|---|---|---|---|---|---|
| Saquon Barkley | 6 | Out (hamstring), week-6 report | 7.57 | 9.82 | 2.19 (stale ESPN Questionable, p 0.29) | 0.00 (p 0.0003) | participation (data) + opportunity |
| Dameon Pierce | 6 | Active; Saquon Out, Bigsby and Shipley on IR | 7.73 | **12.58** | 7.46 | **12.32** (p 0.98) | opportunity: expected 70% of carries vs 11% with everyone available (one earlier game without Saquon) |
| Breece Hall | 6 | Out (quadriceps), missed weeks 4-5 | 12.50 | 12.26 | 0.02 | 0.01 | none material |
| Braelon Allen | 6 | Active; Hall Out | 10.53 | 13.04 | 10.46 | 12.95 | opportunity (71% of carries) |
| Saquon Barkley | 7 (horizon 1 from week 6) | uncertain: no week-7 report | no ifp-2 run | 9.67 (status quo) / 9.47 (scenarios) | n/a | ROS today: p 0.93; table: 0.39 | opportunity; p_play is the ROS issue |
| Dameon Pierce | 7 | depends on Saquon's return | no ifp-2 run | 12.43 status quo / **9.93** weighted by Saquon's 0.39 return rate | n/a | n/a | opportunity scenarios |
| Breece Hall | 7 | uncertain: no week-7 report | ifp-1 run 13.60, p 0.04 (bug: week-6 status applied) | 13.01 / 12.88 | 0.58 (bug) | ROS today: p 0.96; table: 0.40 | bug fixed; participation |

Saquon's week-6 conditional rises because the handoff remembers his healthy lead role (62% of carries
conditional on playing) instead of the two injury-shortened games; it does not matter for week 6 because he
is Out. **Scenario B (Saquon confirmed back in week 7) and the week-7 Breece return are synthetic only**: they
are the mandatory tests in `test_injury_workload.py` (a returning lead reclaims > 55% of carries, the
replacement's share falls by more than half and his `ho_shift_pg` turns negative, the missed week is not a
zero-workload game, the RB budget adds up).

Explanation metadata (`injury_workload.explain`) produces, for example, for Pierce week 6: "Potential expanded
role while Saquon Barkley is unavailable: expected share 70% of team carries vs 11% with everyone available,
from prior usage and the remaining competition." and for Breece week 7: "Availability uncertain: injured as of
the week-6 cutoff; no week-7 report exists yet (return neither assumed nor ruled out). Historical healthy
workload retained despite 2 missed games (last healthy game 2026 week 3)." Nothing is wired into the frontend.

## 9. Limitations and next work

Limitations:

* **No future injury reports.** Week T's report cannot exist at a week-W cutoff. The timeline says so instead
  of guessing; the return-rate table is a frequency, not a recovery date, and drifts upward at h >= 3.
* **Pregame timestamps.** 2025-26 injury reports carry no modification time (retrospective replay); the
  nflverse weekly roster for week W is treated as game-day information and never read for week W.
* **Reserve eligibility** needs the GSIS reserve code; without it a reserve stint has no dated return.
* **Depth charts are not used** (no publication time before 2025, licence unverified). A player with no
  history gets the newcomer prior; a practice-squad call-up therefore starts small until he plays.
* **Small return cohorts.** Lead RBs returning are 3 + 13 confirmation rows: the return effect on the returner
  is not measurable historically.
* **Questionable teammates** stay in the pool at horizon 0 (they usually play); no probability is mixed in.
* Unconditional expected points were not re-evaluated (section 7.4).

Next work, by expected value:

1. **Prospective shadow of iwh-0.1.1**: implemented (section 10). Remaining: run `predict` each week once the
   report is synced (manually for now; not wired into `learning_cycle.py` like ifp-ctx-p1's `ctx_shadow.py`).
2. **ROS availability**: replace the undesignated p_play for players injured at the cutoff with the empirical
   return table (refit per season), plus the teammate-return scenarios; pre-register calibration (Brier, ECE)
   on 2023-24 and a prospective check, then propose to the owner.
3. **Re-project after the final injury report** for teams still pregame (Friday/Saturday/Sunday morning), as a
   new append-only run, so `expected_points` reflects Out designations.
4. Unconditional (`p_play x conditional`) walk-forward evaluation on the roster population.
5. Wire the explanation metadata into the Player Profile once a model using it is approved.

## 10. Prospective shadow of ifp-iwh-0.1.1

**What changed (iwh-0.1.1).** `_Cut.injured_absence` decides whether a missed game is an injury absence, for
both `_participation` and the timeline's `missed_injured`:

| Missed game while... | iwh-0.1.0 | iwh-0.1.1 |
|---|---|---|
| Out or Doubtful | injury absence | injury absence |
| on a reserve list (RES/PUP/NFI) | injury absence | injury absence |
| Questionable | role absence (claim cut) | injury absence |
| on the practice report with no game status | role absence (claim cut) | injury absence |
| listed "not injury related" (rest, personal) | role absence | role absence |
| not on the report at all | role absence | role absence |

Knock-on effects, all intended: a player who missed games Questionable and is back is now
`available_returning` (was `available`, "a role or roster decision"), and at horizon >= 1 or without a week-W
report such a player is `uncertain` (was `available` / `unknown`). `prev_game_out` still means Out/Doubtful.
Tests: `test_questionable_then_inactive_is_an_injury_absence_not_a_role_absence` and the three after it.

**Why prospective only.** 2021-22 selected the adoption and 2023-24 confirmed (and failed) ifp-iwh-0.1.0 once.
Re-running either window with the fix would select or adjudicate on weeks that were already looked at, so
`develop` and `evaluate` refuse under 0.1.1 and no historical number is restated. 2025-26 replays are also
out: their injury reports carry no timestamp.

**Pre-registered in code before any prospective week was forecast or scored**
(`injury_challenger.PROSPECTIVE_RULES`, `PROSPECTIVE_THRESHOLDS`, `prospective_gate`):

* Adoption: RB, WR and TE, ifp-iwh-0.1.0's development result unchanged (no reselection). QB keeps ifp2.
* Evidence: write-once pregame ledger rows from **2026 week 7**, scored once the week is fully final; ifp2 and
  ifp-iwh-0.1.1 fitted in the same run on the same training rows; identical rows only (both predictions
  non-null, the player appeared).
* Per adopted position (`challenger.SHADOW_GATE`): >= 8 fully final weeks, >= 400 matched appearances, MAE diff
  week-clustered 95% CI upper < 0, |bias| not worse by > 0.15, starters-pool MAE not worse; plus (c3) MSE diff CI
  lower bound not above 0.
* Pooled adopted rows (`DECISION_RULES['confirmation']`): (c1) MSE diff < 0 with CI upper < 0; (c7) every
  mechanism cohort with n >= 100 has MSE diff < 0 and |bias| no larger.
* The returning-player cohort (`self_returning_any`, the c7 failure the fix targets) must reach n >= 100 before
  the gate can be met. In the confirmation window it accrued about 6 rows per week under 0.1.0 (204 rows); 0.1.1
  labels more players as returning, but expect this to take most of the remaining season or longer.
* Outcome: `meets_gate` (every check) | `insufficient_data` (weeks, appearances or the returning cohort short) |
  `fails_gate`. `meets_gate` is evidence for an owner review only: no promotion, no registry change.

The first `predict` freezes these rules write-once (`frozen_ifp-iwh-0.1.1.json`, hashed together with the ifp-2
config, the league scoring and the source of the gate and cohort functions). Every later `predict` and
`prospective` must match the hash exactly, so a rule or gate edited after the first ledger stops the shadow
instead of re-scoring it.

**`predict --week W`** (run after the week's report is synced, before kickoff):

* refuses weeks before 2026 week 7, and refuses while the week's injury report is not ingested for every team
  still to play (no override: the ledger is write-once, so a forecast made without the report would be frozen
  degraded);
* excludes teams that have kicked off, checked before the build, after it, and again at write time;
* fits ifp2 and ifp-iwh-0.1.1 in the same run on training rows strictly before W, and writes the fitted models
  write-once (`artifacts/ifp-iwh-0.1.1-sSSSSwWW.json`; an interrupted run reuses them, never refits);
* writes `ledger/sSSSSwWW-pregame.json` once: both models' points per player, the frozen timeline and handoff
  columns the cohorts read, the explanation text, and source hashes (the week's report and its designation
  count, the season's reports through W, rosters, panel, schedule, roster codes, store fingerprint, code hash),
  plus a hash of the rows. A rerun returns the existing ledger and writes nothing.

**`prospective`** scores ledgers whose week is fully final (in the store), whose config hash matches the
frozen spec and whose rows still match their hash. Cohort columns come from the ledger, never recomputed after
the fact. Output: `prospective_ifp-iwh-0.1.1.json` (gate plus the usual cohort report).

## Reproduce

```
cd backend
uv run python ../scripts/injury_workload_research.py build
uv run python ../scripts/injury_workload_research.py develop     # writes the frozen adoption once
uv run python ../scripts/injury_workload_research.py evaluate    # confirmation + exploratory
uv run python ../scripts/injury_workload_research.py ros
uv run python ../scripts/injury_workload_research.py cases
# ifp-iwh-0.1.1 prospective shadow (develop / evaluate above refuse under 0.1.1; use commit d6fed66 for 0.1.0)
uv run python ../scripts/injury_workload_research.py predict --week 7   # each week, after the report sync
uv run python ../scripts/injury_workload_research.py prospective
```

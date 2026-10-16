# Independent Weekly Projection Engine (V1, research)

An NFL-evidence fantasy-point model for QB/RB/WR/TE that **never uses ESPN projections as an
input**. ESPN appears only as an external comparison. It is research output: by default nothing in
lineups, trades, waivers, playoff odds, player scores or market signals reads it. The one opt-in
path is the **projection switch** (header toggle "ESPN | Ours", see "Projection switch" below),
which only changes the weekly numbers on Home, Matchup and Start/Sit.

**Current status (model `ifp-1.0.0`, held-out evaluation on real data, see "Results"):**

| Position | Status | Start/Sit ready |
|---|---|---|
| QB | `validated` vs trailing-average baselines only (not vs ESPN) | no |
| RB | `experimental` (no better than season average) | no |
| WR | `experimental` (2.8% better than baseline; below the 3% bar) | no |
| TE | `experimental` (2.7% better than baseline; below the 3% bar) | no |
| K, D/ST | `unsupported` (no independent model in V1) | no |

No position is Start/Sit ready: there are **zero** completed player-weeks with a pregame ESPN
snapshot, so no claim of beating ESPN is possible yet.

**Participation model (`ipm-1.0.0`, held-out on real data, see "Participation"):** weekly
estimates now come in two forms. `projected_points` is the conditional estimate above ("points if
he appears"). `expected_points = p_play * projected_points` is an **unconditional** expected score,
where `p_play` = P(appears in week W) from pregame roster status, injury report and snap history.

| Position | P(appears) | Expected points | Unconditional MAE vs conditional-as-is |
|---|---|---|---|
| QB | `experimental` (ECE 0.042 > 0.02; Brier beats baselines) | `experimental` | 6.76 -> **4.33** (-36%) |
| RB | `experimental` (ECE 0.025 > 0.02; Brier beats baselines) | `experimental` | 4.03 -> **3.59** (-11%) |
| WR | `validated` | `validated` | 4.15 -> **3.71** (-11%) |
| TE | `validated` | `validated` | 2.95 -> **2.74** (-7%) |

"Validated" here means calibrated and better than simple pregame baselines on held-out
roster-population weeks; it says nothing about ESPN.

**Research challenger `ifp-2.0.0` (shadow, see "Weekly learning and the ifp-2 challenger"):**
injury-aware established workload, previous-season usage priors and recency-weighted training.
It passed its frozen confirmation rules on 2023-24 (pooled MAE 4.031 -> 4.016, 95% CI of the
difference [-0.023, -0.007]) but the gain is ~0.4%, far below the 3% promotion bar. It runs in shadow
only: nothing reads it, `ifp-1.0.0` and its artifacts are unchanged, and only frozen pregame forecasts
from 2026 week 6 onward count as evidence for it.

## Files

| File | Role |
|---|---|
| `backend/app/analytics/league_scoring.py` | ESPN `scoring_items` -> weights on nflverse box-score columns; refuses unsupported rules |
| `backend/app/nfl/history.py`, `scripts/sync_nfl_history.py` | Optional finished-season nflverse ingestion into a separate DB |
| `backend/app/analytics/forecast_features.py` | Point-in-time panel + features |
| `backend/app/analytics/independent_projections.py` | Baselines, ridge models, artifact, explanations |
| `backend/app/analytics/forecast_backtest.py` | Walk-forward validation, selection, held-out evaluation, ESPN matching |
| `backend/app/analytics/independent_projection_store.py` | Live week-W run with availability/ID guards |
| `backend/app/api/independent_projections.py` | Read-only `GET /api/projections/independent` |
| `backend/app/analytics/participation.py` | Roster population, participation features, logistic model, walk-forward evaluation, live P(appears) |
| `backend/app/analytics/promotion_tracker.py`, `backend/app/api/projection_promotion.py` | Read-only `GET /api/projections/independent/promotion` (the tracker on `/data`) |
| `scripts/backtest_independent_projections.py` | `audit`, `select`, `evaluate`, `fit`, `participation-select`, `participation-evaluate`, `participation-fit`, `snap-ablation`, `project` |
| `backend/app/nfl/pbp_exits.py` | Play-by-play in-game injury evidence per player-game (research input) |
| `backend/app/analytics/challenger.py`, `challenger_config.json` | ifp-2 challenger: weighted/robust linear fits, walk-forward specs, cohorts, frozen config, confirmation and shadow criteria |
| `backend/app/analytics/weekly_learning.py`, `scripts/weekly_learning.py` | Weekly cycle: readiness gate, versioned artifacts, write-once forecast ledger, prospective evaluation |
| `backend/app/analytics/model_registry.py`, `model_registry.json`, `scripts/model_registry.py` | Approval manifest, integrity checks, dry-run promotion gate and rollback planner (research only; `docs/model_registry.md`) |
| `scripts/challenger_research.py` | `store`, `develop`, `freeze`, `confirm`, `exploratory`, `exits-audit`, `cases` |

Added dependency: **numpy** (closed-form ridge, bootstrap). No scikit-learn, no gradient boosting,
no cloud services, no LLM numbers.

## Runbook

From `fantasy-intel/backend` (all outputs land in git-ignored `data/`):

```bash
uv run python ../scripts/sync_nfl_history.py --seasons 2019-2025                 # ~20 s, ~60 MB, separate DB
uv run python ../scripts/backtest_independent_projections.py audit
uv run python ../scripts/backtest_independent_projections.py select              # validation seasons only
uv run python ../scripts/backtest_independent_projections.py evaluate            # frozen selection -> held-out
uv run python ../scripts/backtest_independent_projections.py fit                 # data/models/independent_projections_ifp-1.0.0.json
uv run python ../scripts/backtest_independent_projections.py participation-select    # validation only
uv run python ../scripts/backtest_independent_projections.py participation-evaluate  # frozen -> held-out
uv run python ../scripts/backtest_independent_projections.py participation-fit       # data/models/participation_ipm-1.0.0.json
uv run python ../scripts/backtest_independent_projections.py snap-ablation           # validation-only diagnostic
uv run python ../scripts/backtest_independent_projections.py project --week 7    # writes the two live tables
```

`select`/`evaluate`/`fit`/`audit` open the live DB read-only. Only `project` writes, and only the
tables `independent_projection_runs` and `independent_player_projections` (create-only, no schema
version bump). `evaluate` refuses a selection made on other seasons or other scoring; `fit` refuses a
held-out report that does not hash-match the current `selection.json`. The participation commands
chain the same way: `participation-select` records the hash of `selection.json` (the conditional
model it multiplies), `participation-evaluate` refuses a stale participation selection, and
`participation-fit` refuses a report of another selection. `project` applies the participation
artifact when `data/models/participation_ipm-1.0.0.json` exists. Override paths with
`--live-db`, `--history-db`, `--out` or `NFL_HISTORY_DB_PATH` / `INDEPENDENT_MODEL_DIR`.

## Data audit (real local data, 2026-10-14)

Live DB (`data/fantasy.duckdb`): **2026 only**, NFL weeks 1-5 complete (79 final games), 2,022
QB/RB/WR/TE player-games. ESPN projection history has **one** snapshot date (2026-10-14) covering
weeks 6-17; 525 week-6 player-weeks are pregame, **0** have a completed actual. ESPN weekly
`projected_points` for weeks 1-5 were captured after kickoff and are never used. ID map: 1,017
high-confidence ESPN->GSIS links, 3 unmatched, 32 D/ST.

History DB (via `sync_nfl_history.py --seasons 2019-2025`):

| Season | Player-games | QB | RB | WR | TE | xFP cov. | Snap cov. | PBP cov. |
|---|---|---|---|---|---|---|---|---|
| 2019 | 6,220 | 598 | 1,713 | 2,489 | 1,420 | 80% | 99.9% | 99.6% |
| 2020 | 6,505 | 631 | 1,784 | 2,559 | 1,531 | 79% | 99.95% | 100% |
| 2021 | 6,883 | 670 | 1,801 | 2,772 | 1,640 | 78% | 99.9% | 100% |
| 2022 | 6,807 | 647 | 1,840 | 2,679 | 1,641 | 79% | 99.99% | 100% |
| 2023 | 6,834 | 680 | 1,737 | 2,803 | 1,614 | 78% | 99.9% | 100% |
| 2024 | 6,816 | 682 | 1,737 | 2,735 | 1,662 | 78% | 99.9% | 100% |
| 2025 | 6,878 | 674 | 1,749 | 2,776 | 1,679 | 78% | 99.8% | 100% |
| 2026 (live) | 2,022 | 186 | 517 | 804 | 515 | 77% | 99.75% | 100% |

xFP is missing for ~22% of appearances, mostly snap-only appearances with no opportunity; it is
imputed with an indicator, never zero-filled. Not available and not modeled: route participation,
coverage splits, pressure-adjusted QB metrics, injury probabilities, depth-chart intent, betting
lines (nflverse stores closing lines, which post-date a midweek cutoff).

## League scoring

`resolve_skill_scoring` maps ESPN stat ids to nflverse columns: 3 passing yards, 4 passing TD,
19/26/44 two-point conversions, 20 interceptions, 24/42 rushing/receiving yards, 25/43 TDs,
53 receptions, 63 fumble-recovery TD, 72 fumbles lost (`fumbles_lost_total`: ESPN counts
special-teams fumbles), 101+102 return TDs (`special_teams_tds`, only when equal).
Kicker statistics and D/ST-only overrides (slot 16) never score offense. Defensive-return events
with non-zero base points (93, 103, 104, 206, 209) are listed as **unmodeled**.

Refused (model unavailable, `coverage_status: unsupported_scoring`): yardage/TD-length bonuses,
first downs, per-target or per-carry points, any slot-specific override other than D/ST (e.g.
TE premium), reverse items, unequal kick/punt return TDs. Half-PPR or standard work because the
weights change, and the **scoring fingerprint** changes: an artifact trained under one
fingerprint refuses leagues with another.

**Verification:** for 2026 weeks 1-5, computed league points match ESPN's stored actuals on
**1,700 of 1,701** high-confidence player-weeks (99.94%; QB/RB/TE 100%, one WR off by 2.0).

## Point-in-time features

A forecast for (season S, week W) uses only games with (season, week) < (S, W), from S and S-1.
`features_for_week` applies this filter itself. Opponent and team context use only that
season's games before W. The target week contributes only team, opponent and home/away.

| Feature | Meaning | Source |
|---|---|---|
| `pts_recent` | League pts/game, last <=5 games, shrunk 2 games toward the position mean | box score + scoring |
| `pts_l3`, `pts_season`, `pts_prev_season` | Last-3 mean; season mean (shrunk); previous season | box score |
| `pts_std` | Volatility over last <=5 games | derived |
| `xfp_recent` | nflverse xFP/game (nflverse PPR units), shrunk | `nfl_ff_opportunity` |
| `pts_minus_xfp` | Efficiency vs xFP (unit mix noted; elite players can sustain it) | derived |
| `targets_pg`, `carries_pg`, `pass_att_pg`, `air_yards_pg` | Per game, last <=5 | box score |
| `target_share`, `rush_share` | Player / team totals over the window | box score + `nfl_team_week` |
| `rz_opps_pg`, `gl_carries_pg` | Red-zone targets+rushes, inside-5 rushes | `nfl_pbp_player_week` |
| `snap_pct`, `snap_trend`, `snap_last` | Mean snap share; last game minus earlier games; last game | `nfl_snap_counts` |
| `injury_exit_last` | 1 if the last game was a snap drop (`snap_trend <= -0.15`) **and** the player is on an injury report (any listing except "not injury related") within 2 weeks after it, up to the target week's pregame report | `nfl_snap_counts` + `nfl_injuries` |
| `snap_trend_role` | `snap_trend` with injury exits removed (null when `injury_exit_last = 1`) | derived |
| `opp_trend` | Last game's targets+carries minus earlier mean | box score |
| `games_recent`, `games_season`, `weeks_since_last`, `team_changed` | Sample and role-change context | derived |
| `team_pts_pg` | Team points/game vs league (shrunk 3 games) | `nfl_schedules` |
| `opp_pos_allowed` | Opponent league pts allowed to the position vs league (shrunk 4 games; 0 in week 1) | panel |
| `opp_pass_epa`, `opp_rush_epa` | Opponent EPA/play allowed vs league (shrunk 150 plays) | `nfl_defense_week` |
| `is_home` | Home game | `nfl_schedules` |
| `xfp_missing`, `snap_missing`, `rz_missing` | No recent coverage | derived |

A snap-only appearance is a measured zero for box-score stats; a missing team play-by-play game
leaves red-zone fields null. Features are rounded to 1e-10 so runs are bit-reproducible.

## Models

* **A1 recent_avg**: raw league pts/game over the last <=5 games.
* **A2 season_avg**: raw season pts/game, previous season if no current games.
* **B opportunity**: per-position ridge on opportunity/role features only.
* **C context**: B plus production, efficiency, role change, team offense, opponent, home, flags.

Ridge on standardized features, unpenalized intercept, penalty = lambda * n, missing values imputed
with the **training** mean plus a `__missing` indicator, predictions floored at 0. Chosen over
boosted trees because the per-position samples (4k-17k rows) are modest, coefficients give exact
additive explanations, and nothing larger was justified. Players with no game in the lookback
window abstain (no position-average guess).

## Validation protocol

* Feature warm-up 2019; training from 2020; **validation 2023-2024** (lambda grid, B vs C,
  interval bands); **held-out 2025 + 2026 weeks 1-5**.
* Walk-forward: every test week refits on strictly earlier weeks (expanding window).
* Population: player-weeks where the player appeared. Estimates are **conditional on playing**.
* `PROMOTION_CRITERIA` (in `forecast_backtest.py`, committed before the held-out run):
  >= 400 held-out observations; >= 3% MAE improvement over the better trailing baseline; week-
  clustered bootstrap 95% CI of the MAE difference entirely below 0 (1,000 resamples, seed
  20261014); 80% intervals published only if held-out coverage is 75-85%; Start/Sit readiness
  additionally needs >= 300 matched pregame ESPN observations per position with primary MAE <= ESPN.

## Results (REAL data, held-out, run once with the frozen selection)

Selection (validation 2023-24): context model C primary everywhere (validation MAE
QB 6.06 / RB 4.02 / WR 4.24 / TE 2.86 vs B 6.29 / 4.16 / 4.36 / 2.95).

Held-out, same player-weeks for every model (common support):

| Pos | n | Recent avg | Season avg | Opportunity (B) | Context (C) | Bias C | RMSE C | 95% CI C - best baseline |
|---|---|---|---|---|---|---|---|---|
| QB | 834 | 6.95 | 6.97 | 6.67 | **6.55** | -0.07 | 8.14 | [-0.57, -0.23] |
| RB | 2,196 | 4.21 | **4.07** | 4.20 | 4.10 | -0.03 | 5.89 | [-0.09, +0.12] |
| WR | 3,477 | 4.20 | 4.29 | 4.20 | **4.08** | -0.17 | 5.74 | [-0.19, -0.05] |
| TE | 2,138 | 3.20 | 3.13 | 3.14 | **3.05** | +0.03 | 4.58 | [-0.18, -0.01] |

Bias = mean(actual - projected). Coverage 96.9-97.5% (the rest abstain: 26 QB, 70 RB, 103 WR, 56 TE
debut/long-absence appearances). By season, C beat both baselines for WR/TE in 2025 and 2026; RB
lost to the 2025 season average. Cohorts: C is better for projections >= 10 at every position, but
worse than baselines for low-volume RBs (< 5 opportunities/game) and low-sample RB/WR/TE (< 3 games).

80% interval coverage on held-out: QB 74.9% (withheld: outside 75-85%), RB 79.3%, WR 80.1%,
TE 80.0%. Intervals are prediction intervals for one week's score, not confidence intervals for a mean.

**Start/Sit pairs** (same position-week, pregame pool QB 24 / RB 48 / WR 60 / TE 24 by recent avg;
78,995 pairs, 355 actual ties skipped): pick-accuracy recent 62.5%, season 63.0%, opportunity 64.0%,
**context 65.2%**; mean regret 2.82 / 2.82 / 2.64 / **2.51** pts. Close calls (recent averages within
3 pts, 31,155 pairs): 54.4% / 56.7% / 58.2% / **60.1%**.

**Real league rosters** (2026 weeks 1-5, same-position players on the same fantasy roster,
contemporaneous lineup snapshots, 1,012 pairs): recent 64.6%, season 62.3%, opportunity 65.0%,
context 64.7%. No meaningful edge on this small sample.

**ESPN comparison: 0 matched observations.** Nothing is claimed about ESPN.

Runtime (Apple silicon laptop): history ingestion 17 s; feature store 42.7k rows in 3.3 s;
selection 4.4 s; held-out evaluation 5.4 s; final fit 3.4 s; live week run 0.45 s for 657
players; API read ~0.12 s. Artifact: 29 KB JSON (coefficients only, no player data).

## Participation (P(appears), model `ipm-1.0.0`)

The conditional models above are trained and evaluated only on players who appeared, so a healthy
backup QB got ~10-12 points "if he plays" while ESPN projected 0. The participation model estimates
P(appears) so the engine can report an unconditional expected score.

**Population (roster-based).** One row per player-week on the nflverse weekly roster with status
ACT or INA (the game-day 53), position QB/RB/WR/TE (FB -> RB), team playing that week, and >= 1
game in the conditional model's lookback window. Label `played` = the player appears (box-score row
or offensive snaps > 0, the same definition as the conditional model's training rows); otherwise
his league points are 0. 2020-2026 week 5: 51,124 rows; appearance rate QB 55%, RB 82%, WR 87%,
TE 89% (the conditional model's training rows are the appearing subset).

**Leakage rules.**

* The week-W roster status defines the population only. **ACT vs INA is the game-day inactive
  decision**: on real data no INA player ever appears (0 of 7,674 INA skill rows; 90% of ACT rows
  appear), so using it would be the label itself. It is never a feature; a test flips every
  week-W ACT <-> INA and requires identical features.
* Box scores, snaps and roster statuses: weeks strictly before W only (tested by corrupting
  every row at or after W).
* The week-W injury report is a pregame input. nflverse `date_modified` shows 78% of timestamped
  rows (66% of all) last modified on a Friday; the 22 rows (8 at skill positions) modified at or after kickoff are
  **dropped** at load time. Rows without `date_modified` (the live table) are kept.
* Depth order is ranked from earlier games; who is on the week-W 53 is a pregame fact.

**Features** (`participation.FEATURE_DOCS`): week-W designation (Out / Doubtful / Questionable)
and final practice status (DNP / Limited); Out the previous team week; appeared in the team's last
game; share of the team's last <= 4 games played; previous-season appearance share; latest
pre-week roster status (INA / reserve / off the 53); returned from a reserve list since his last
game; depth order at his position on the team (1 / 2 / 3+, by recent snap share, then xFP, then
points); a teammate ahead designated Out/Doubtful or Questionable; snap share (recent, last game),
`snap_trend_role`, `injury_exit_last`, games recent/season, weeks since last game, team change,
xFP. Missing values: training-mean imputation plus indicator, as in the ridge models.

**Model.** Per-position L2 logistic regression (Newton steps, numpy; penalty lambda * n on
standardized coefficients, intercept unpenalized). Chosen over trees for the same reasons as the
ridge models (modest samples, additive explanations). Explanations list the largest logit
contributions relative to the training-average player.

**Protocol.** Identical seasons to the conditional model: training from 2020, lambda selected by
validation log loss on 2023-2024, held-out 2025 + 2026 weeks 1-5, walk-forward refits every week
on strictly earlier weeks. The conditional estimate in every evaluation row is the frozen
`ifp-1.0.0` selection refit walk-forward on appearance rows (exactly the PR #22 protocol).
`PARTICIPATION_CRITERIA` were committed (commit `4e45304`) before the held-out run:
>= 400 held-out rows; Brier below the better of two simple baselines with a week-clustered
bootstrap 95% CI entirely below 0; ECE <= 0.02 over 10 equal-width bins and no bin with n >= 100
off by more than 0.08. Expected points are `validated` only if participation is validated **and**
unconditional MAE improves >= 3% over the conditional estimate with a CI below 0.

Baselines: `always_plays` (1.0, what the conditional estimate implicitly assumes);
`designation_rate` (training appearance rate per week-W designation); `trailing_appearance`
(share of the team's recent games played, shrunk one game toward the base rate).

### Participation results (REAL data, held-out, run once)

Validation (2023-24) selected lambda QB 0.003, RB 0.0001, WR 0.0001, TE 0.001.

Probability quality on held-out roster rows (Brier, lower is better; log loss in parentheses):

| Pos | n | Appeared | Always plays | Designation rate | Trailing appearance | **Model** | Model - best baseline (95% CI) | ECE | Status |
|---|---|---|---|---|---|---|---|---|---|
| QB | 1,544 | 54.9% | 0.451 | 0.235 | 0.163 | **0.112** (0.355) | -0.051 [-0.062, -0.041] | 0.042 | experimental |
| RB | 2,593 | 84.9% | 0.151 | 0.102 | 0.108 | **0.071** (0.234) | -0.031 [-0.039, -0.023] | 0.025 | experimental |
| WR | 3,975 | 87.0% | 0.130 | 0.070 | 0.118 | **0.056** (0.198) | -0.015 [-0.019, -0.011] | 0.012 | validated |
| TE | 2,424 | 87.6% | 0.124 | 0.078 | 0.102 | **0.054** (0.177) | -0.024 [-0.031, -0.017] | 0.015 | validated |

QB and RB fail **only** the calibration bar. QB reliability (held-out): predictions of 0.4-0.6
came true 30-36% of the time (n = 116, overconfident in the middle), 0.1-0.2 came true 21% (n = 242,
underconfident for backups); the extremes are well calibrated (0.96 predicted vs 0.98 observed,
n = 508). RB's largest gaps are in sparse bins (0.1-0.2 and 0.3-0.4, n <= 49). Recalibration is a
follow-up; the frozen statuses stand.

Calibration research (`docs/research/participation_calibration.md`, shadow only): walk-forward
Platt / shrunk Platt / isotonic maps fitted only on earlier out-of-sample predictions. Only QB
isotonic passes the pre-registered rule (log loss and Brier CIs below 0, ECE 0.046 -> 0.015 in
development); it does not improve unconditional fantasy points, so `ipm-1.0.0` is unchanged and the
map is kept as a shadow candidate.

Unconditional league points (actual = points if he appeared, else 0), same rows:

| Pos | Conditional as-is | Out rule (Out -> 0) | Best baseline x conditional | **Expected points** | vs conditional (95% CI) | vs Out rule (95% CI) |
|---|---|---|---|---|---|---|
| QB | 6.76 (bias -3.26) | 6.26 | 4.98 | **4.33** (bias +0.12) | -36% [-2.64, -2.24] | -31% [-2.11, -1.76] |
| RB | 4.03 (bias -0.59) | 3.84 | 3.67 | **3.59** (bias +0.02) | -11% [-0.48, -0.38] | -6.3% [-0.26, -0.22] |
| WR | 4.15 (bias -0.74) | 3.85 | 3.72 | **3.71** (bias -0.16) | -11% [-0.50, -0.39] | -3.6% [-0.16, -0.12] |
| TE | 2.95 (bias -0.23) | 2.80 | 2.73 | **2.74** (bias +0.07) | -7.2% [-0.26, -0.17] | -2.2% [-0.08, -0.04] |
| All | 4.23 (RMSE 5.96) | 3.96 | 3.80 | **3.55** (RMSE 5.41) | -16% | |

MAE in league points; CIs are of the MAE difference in points. Bias = mean(actual - estimate): the
conditional estimate over-projects everyone who might sit. The "Out rule" approximates what the
live run did before (withhold ESPN OUT, assume everyone else plays). Note that expected value
minimizes squared error, not absolute error; RMSE improves too (all positions 5.96 -> 5.41).

Cohorts (held-out, all positions agree in direction): designated Out/Doubtful players appeared 0
times in 436 rows (model mean 0.1-1.8%); Questionable appeared 24% (QB) to 71% (RB); depth-2+ QBs
appeared 29%, and 63% when the starter was designated Out/Doubtful (model 60%); players coming off
an injury exit appeared 33-68%. Model Brier by season (2025 / 2026): QB 0.118 / 0.089, RB 0.069 /
0.075, WR 0.057 / 0.051, TE 0.054 / 0.051.

### Injury exit vs role demotion (`snap_trend`)

`snap_trend` treated a player who left the last game hurt like a player whose role shrank.
`injury_exit_last` labels a last-game snap drop (>= 15 points below the earlier recent mean) as an
injury exit when an injury report lists him within two weeks after it (rest listings excluded),
using reports only up to the target week's pregame report; `snap_trend_role` keeps the role part.
Evidence (2020-2024, 7,183 drops): after an injury exit the player appeared next week 52% of the
time (role drops: 74%); when he did appear, his snap share was back within 10 points of the
pre-drop level 48% of the time (role drops: 37%). Separation is real but partial: injury reports
also list players for unrelated reasons, and blowout/garbage-time drops look like role drops.

Use: `injury_exit_last` and `snap_trend_role` are participation-model inputs, and live flags now
say "treated as an in-game injury exit, not a role demotion" or "possible role demotion". The
conditional model `ifp-1.0.0` is **unchanged**: a validation-only ablation (`snap-ablation`,
2023-24) replacing `snap_trend` by `snap_trend_role` + `injury_exit_last` in model C moved
validation MAE by -0.006 / -0.004 / -0.002 / 0.000 (QB/RB/WR/TE), every 95% CI spanning 0, and was
slightly worse on the injury-exit rows themselves. The held-out seasons were not used for it.

## Weekly evidence routine and promotion tracker

The ESPN criterion can only be met with ESPN projections captured **before** kickoff, and ESPN
history only began on 2026-10-14. Nothing backfills a missed week, so every week:

1. **Before Thursday's first kickoff**, capture a pregame ESPN snapshot (from `backend/`):

   ```bash
   uv run python ../scripts/sync_espn.py
   ```

   Syncing again before Sunday is fine and keeps the comparison current: each player is compared
   with the last snapshot taken before *his own* kickoff (`forecast_backtest.espn_pregame`).
   `espn_projection_history` keeps one snapshot per calendar day, so a sync later the same day
   replaces that day's earlier snapshot. A Thursday sync after the Thursday game has kicked off
   therefore leaves that game's players with the previous day's snapshot; sync on Wednesday as well
   if you want a fallback.
2. **Store the week's independent projection** (needs the fitted artifacts in `data/models`, see
   "Runbook"): `uv run python ../scripts/backtest_independent_projections.py project --week N`.
   Its features use only games completed before week N, but its weights come from the artifact:
   `fit` trains on every completed week. A run therefore counts as evidence only if its artifact
   was fitted **before week N's first kickoff** (and under the current model version and league
   scoring); a backfill with a later artifact is listed as excluded. Without a counted run a week
   contributes no matched observation to the tracker.
3. After the week's games are final and nflverse is synced (`sync_nfl.py`), the tracker counts the
   new matched player-weeks automatically.

The **Data quality page (`/data`) section "Independent projections: path to promotion"** reads
`GET /api/projections/independent/promotion` (`independent-promotion/v1`, read-only):

* per position, the held-out status, relative MAE improvement vs the `PROMOTION_CRITERIA` bar,
  held-out MAE and n, copied from the evaluation stored with the latest live run of the current
  model version under the current league scoring (nothing is recomputed; with no such run these
  are null and the page says why);
* matched pregame-ESPN observations so far vs `espn_min_matched_per_position`, computed with
  `espn_pregame` + `espn_comparison` on counted stored weekly projections (`pred_primary`; see step 2)
  and completed
  appearances (league points from nflverse box scores), plus the pool of completed player-weeks
  with a pregame ESPN snapshot whether or not a projection was stored; MAE ours vs ESPN once
  there is at least one matched row;
* whether the current week already has an ESPN snapshot taken before its first kickoff (a later
  snapshot still counts for players whose own game kicks off after it).

The tracker never promotes anything: once a position reaches the matched count, rerun `evaluate`,
and only after a position passes the frozen criteria **and the user approves** may its
projections reach lineups, start/sit, trades or waivers. The isolation test in
`tests/test_independent_projections.py` keeps every other module from importing the tracker or
the projection tables. The hosted API bundle ships without polars, so there the endpoint answers
`available: false` instead of a copy of the criteria.

## Projection switch (opt-in weekly view)

A header toggle (**ESPN | Ours**, in the menu drawer on phones) stores the visitor's choice in the
`fi_projection` cookie. With "Ours", Home, Matchup and Start/Sit request `source=independent`
(`GET /api/start-sit`, `GET /api/matchup`); ESPN stays the default everywhere.
`app/analytics/projection_source.py` turns the stored run for the week into lineup numbers:

* `ours`: `expected_points` (unconditional, like ESPN's weekly projection);
* `ours_zero`: 0.0 for a bye or a row withheld as unavailable;
* `espn`: ESPN's number where no independent estimate exists (K, D/ST, abstentions, uncertain ID
  mapping, no participation estimate), tagged "ESPN" in the UI.

Only a run of the current model version under the league's current scoring is used; otherwise the
response says `available: false` and shows ESPN. Start/Sit and Matchup rerun the same lineup
optimizer on these numbers (Matchup computes both teams' lineups on the fly instead of reading
`team_week_lineup_projections`). Availability warnings still quote ESPN's number, because they say
so by name. Rest-of-season features (trades, waivers, team value, playoff race) stay on ESPN: the
model projects one week at a time. The switch is a view, not a promotion: the frozen
`PROMOTION_CRITERIA` and the tracker are unchanged.

## Live runs and abstention

Week-W rows (`coverage_status`): `projected`; `abstain_no_history`; `bye`;
`withheld_unavailable` (NFL-ineligible for W per `player_availability`, ESPN OUT / INJURY_RESERVE /
SUSPENSION, or latest weekly roster status not ACT/INA, e.g. exempt list); `uncertain_id_mapping`
(anything but a high-confidence, conflict-free ESPN->GSIS link); `unsupported_position` (K, D/ST);
`unsupported_scoring`; `no_model`. Fantasy IR and ESPN QUESTIONABLE/DOUBTFUL are flags only.

**Participation in live runs.** When the participation artifact exists, `project` computes
P(appears) for every player whose latest weekly roster row of the season (week <= W) is ACT/INA
on a team playing in W (the week-W roster may not be posted yet), ranks depth on that full
population, and stores `p_play` and `expected_points = p_play * projected_points` on every
`projected` row (null elsewhere; a projected player without an ACT/INA roster row gets a note and
no estimate). The nflverse week-W injury report usually syncs late in the week (on 2026-10-14 it
covered 2 of 32 teams with no designations yet). For a team with no synced nflverse designation the
run falls back to the **ESPN injury status** (OUT / DOUBTFUL / QUESTIONABLE, high-confidence ID
map) as that team's designation and flags every affected row; this is injury status, never an ESPN
projection, and the model was trained on nflverse designations, so the fallback itself is
unvalidated. Teams with no designation from either source are flagged too.

Real week-6 run (2026-10-14 data, 386 projected rows, 0.75 s): the 73 rows where ESPN projects 0
average P(appears) 0.51 and expected points 1.46 (conditional estimate 3.85), so the mean gap to
ESPN on those rows falls from 3.85 to 1.46 points. Drew Lock (SEA backup, ESPN Questionable):
P 0.05, expected 0.58 (conditional 10.77). Tyler Huntley (BAL): P 0.71, expected 8.84 (conditional
12.47) because Lamar Jackson is Questionable and Huntley played most of the last game; ESPN says
0. On the 313 rows where ESPN projects > 0 the mean absolute gap to ESPN rises slightly (1.19
conditional -> 1.35 expected): ESPN's positive projections behave more like "if he plays". None
of this is an accuracy claim; no week-6 game is final.

## API contract (`independent-projections/v2`)

v2 = v1 plus `independent.expected_points`, `independent.participation`,
`comparison.expected_minus_espn`, `comparison.expected_comparable` and `run.participation`
(additive; v1 fields keep their meaning).

`GET /api/projections/independent?week=7[&position=WR][&team_id=3]` (default week: league
`current_week`). 200 with `available: false` when no run exists; 422 for a bad position.

```json
{
  "schema_version": "independent-projections/v2",
  "league": {"id": 123, "season": 2026}, "week": 7,
  "available": true, "unavailable_reason": null, "notes": ["..."],
  "run": {"run_id": "ifp-2026w7-...", "model_version": "ifp-1.0.0", "artifact_created_at": "...",
          "created_at": "...", "scoring_fingerprint": "e165b59686110c7d",
          "feature_cutoff": {"data_through_season": 2026, "data_through_week": 6,
                             "rule": "only NFL games completed before the target week"},
          "nfl_synced_at": "...", "heldout_evaluation": {"...": "aggregates"},
          "participation": {"model_version": "ipm-1.0.0", "status": {"WR": {"participation": "validated",
                            "expected_points": "validated", "reasons": []}}, "heldout_evaluation": {"...": "aggregates"}}},
  "model_status": {"WR": {"status": "experimental", "validated_for": null, "start_sit_ready": false,
                          "intervals_published": true, "reasons": ["relative MAE improvement 0.0276 < 0.03", "..."]}},
  "players": [{
    "espn_player_id": 1001, "gsis_id": "00-0000001", "name": "Example WR", "position": "WR",
    "nfl_team": "AAA", "opponent": "BBB", "is_home": true, "fantasy_team_id": 3, "injury_status": "ACTIVE",
    "independent": {
      "projected_points": 11.4, "interval_80": {"low": 3.1, "high": 22.7, "nominal_coverage": 0.8},
      "model": "context", "model_version": "ifp-1.0.0", "model_status": "experimental",
      "start_sit_ready": false, "coverage_status": "projected", "games_used": 5,
      "latest_usage": {"season": 2026, "week": 6}, "missing_inputs": [], "flags": [],
      "explanation": [{"feature": "target_share", "value": 0.27, "training_mean": 0.119,
                       "contribution": 1.9, "text": "target share: 0.270 (training average 0.119)"}],
      "is_espn_fallback": false,
      "expected_points": 10.9,
      "participation": {"p_play": 0.956, "model_version": "ipm-1.0.0", "model_status": "validated",
                        "expected_points_status": "validated", "depth_rank": 1,
                        "explanation": [{"feature": "played_last_team_game", "logit_contribution": 0.8,
                                         "text": "played in the team's last game: 1", "direction": "raises"}],
                        "flags": [], "note": null}},
    "espn": {"projected_points": 9.8, "synced_at": "2026-10-20 12:00:00"},
    "comparison": {"independent_minus_espn": 1.6, "comparable": true, "comparability_note": null,
                   "expected_minus_espn": 1.1, "expected_comparable": true,
                   "evidence": [{"kind": "model_input", "feature": "target_share", "contribution": 1.9,
                                 "text": "target share: 0.270 (training average 0.119)"}]}
  }]
}
```

Rules for consumers: `null` means unavailable, never zero. `independent` is never filled from
ESPN (`is_espn_fallback` is always false). Use `independent_minus_espn` only when
`comparable` is true: ESPN projects 0 for players it does not expect to play, while the
independent estimate assumes the player plays. `expected_minus_espn` compares two unconditional
numbers and is available whenever both exist; check `participation.model_status` (QB/RB are
`experimental`) before trusting it. `evidence` lists the model's own largest input
contributions (points relative to the training-average player) plus data-quality flags; it never
explains ESPN's number. Players are ordered by independent projection (nulls last), then id.

## Weekly learning and the ifp-2 challenger (research, shadow)

Full evidence, tables and examples: `docs/research/ifp2_weekly_learning_report.md`.

### What the champion did with completed weeks

`ifp-1.0.0` learned from a finished week only when someone ran `fit`, which retrains the frozen
selection on every completed week and **overwrites** `independent_projections_ifp-1.0.0.json`.
There was no completeness gate (an unfinished Monday game was simply absent), no record of which
artifact produced a stored forecast beyond `artifact_created_at`, and `project` replaces the week's
rows, so a rerun after kickoff erased the pregame forecast. The weekly pipeline below fixes this
without touching `fit`, `project` or the live tables.

### Weekly cycle (`scripts/weekly_learning.py`, from `backend/`)

| When (US Eastern) | Command | What it does |
|---|---|---|
| Tue/Wed, after Monday night is final and nflverse has published | `uv run python ../scripts/sync_nfl.py` | Existing nflverse sync (box scores, snaps, xFP, injuries, rosters) |
| same | `uv run python ../scripts/weekly_learning.py sync-exits` | Play-by-play in-game injury evidence for the season's final weeks (`<out>/pbp_exits/<season>.parquet`) |
| same | `... status --week W` | Read-only readiness: every REG game before W final; box scores and snaps for every team that played; nflverse expected points for >= 50% of appearances; W has not kicked off |
| Wed, before Thursday's kickoff | `... train --week W` | Champion = `ifp-1.0.0` frozen selection refit (bit-identical to `fit`); challenger = frozen `ifp-2.0.0` config; both on FINAL weeks < W |
| Wed-Sun, before kickoff (rerun after late injury news is fine) | `... predict --week W` | Frozen pregame forecasts for every ACT/INA roster player on a team playing in W: champion, challenger, trailing baselines, P(appears) |
| any time | `... evaluate` | Prospective accuracy of frozen pregame forecasts whose own games are final and synced; only fully final weeks count toward `SHADOW_PROMOTION_CRITERIA` (see "Prospective evaluation: outcome finality") |
| | `... cycle --week W` | `sync-exits`, `status`, `train`, `predict`, `evaluate` in order; stops at the first refusal |

One-time history: `... sync-exits --history-seasons 2019-2025`. Defaults: `--out data/models/weekly`
(`WEEKLY_MODEL_DIR`, git-ignored), production files read from `--ifp-dir data/models` (read-only).
Exit code 3 and `NOT READY: ...` on any refusal.

Safety rules (each is tested in `tests/test_weekly_learning.py`):

* A week enters training only when **every** game of it is final; a partly final week is excluded
  whole. Training rows have `(season, week) < W` (asserted) and features from earlier games only.
* `train`/`predict` refuse after week W's first kickoff unless `--retrospective`; retrospective
  artifacts and runs are labeled, get no P(appears) (the participation artifact was fitted later),
  and are reported separately as "not evidence".
* Artifacts are content-addressed: `artifacts/<version>/s<season>w<week>-<sha>.json`, written once.
  Retraining on identical inputs reproduces the same id; changed inputs add a new file. Provenance:
  target week, data cutoff, latest included week, training seasons, rows per position, data hash,
  configuration hash, scoring fingerprint, play-by-play coverage, retrospective flag.
* A ledger run (`ledger/s<season>w<week>/<run_id>.json`) is never overwritten. An identical rerun is
  a no-op; a changed input (e.g. a Saturday injury report) adds a run. Evaluation uses, per player,
  the latest run created **before his own kickoff** by artifacts whose data cutoff precedes W.
* Scoring fingerprint, artifact schema and data cutoff are checked before every prediction.
* Nothing in `app/` imports the pipeline or the challenger (tested). Weekly retraining never
  re-tunes: penalties, features and interval bands come from committed configuration.

The participation model is not retrained weekly here (`participation-fit` is unchanged); its
`expected_points` are recorded as is.

### Automated weekly cycle (`scripts/learning_cycle.py`)

**Where it runs.** The cycle needs the writable `data/fantasy.duckdb`, `data/nfl_history.duckdb`
and the git-ignored `data/models/` (production `ifp-1.0.0` files; weekly artifacts, ledger and
reports in `data/models/weekly/`). They exist only on the laptop that runs the syncs. MotherDuck
holds a read-only published copy of the live database (no history database, no model files) and
Vercel serves the API from it; GitHub Actions has neither. So the cycle is a second per-user launchd
agent on that Mac (`scripts/launchd/com.fantasy-intel.weekly-learning.plist`, Tue/Wed/Thu 07:00
Pacific), separate from `com.fantasy-intel.refresh` (ESPN snapshots, analytics, MotherDuck), which
is unchanged and still owns the pregame ESPN snapshots. Moving the cycle to the cloud would need the
history database and model directory in persistent hosted storage: new infrastructure, not done here.

**One cycle** (`learning_cycle.py run`; every stage reuses a `weekly_learning.py` command):

| Stage | What | Stops the cycle when |
|---|---|---|
| preflight | live DB, history DB, production `selection.json` / `heldout_report.json`, writable `--out`; refuses hosted mode (`MOTHERDUCK_DATABASE`) | anything missing (exit 5, with the action to take) |
| lock | `<out>/locks/cycle.lock` (pid, host, start); a dead or > 6 h old lock is taken over | another cycle is running (exit 4) |
| sync_nfl, sync_exits | existing `sync_nfl.py` (output to `<out>/logs/`) and `weekly_learning.py sync-exits` (`--skip-sync` skips both) | a sync fails (exit 1) |
| target week | first REG week whose first kickoff (nflverse gameday + gametime, US Eastern, stored in UTC) is after now | no week left (exit 0) |
| readiness | `weekly_learning.readiness`: every earlier game final, box scores / snaps / expected points synced, target not kicked off | data not ready (exit 3: retry before the reported kickoff) |
| evaluate | `weekly_learning.py evaluate` (finality-aware, PRs above) | an error (exit 1) |
| train | refit champion and challenger with frozen configuration, unless the **training fingerprint** (every training row, label and feature; challenger config hash; production selection and report bytes; scoring fingerprint; versions; target) matches the previous cycle's: then the same artifacts are reused. `--force-retrain` always refits | not pregame any more, or an error |
| predict | `weekly_learning.py predict` with the exact artifacts of the train stage (not "latest"), pregame only | the target kicked off meanwhile (exit 3) |

A stage failure stops every later stage. Reports: `<out>/reports/cycle-<stamp>-<pid>.json`
(write-once) and `<out>/status/latest-cycle.json`: status (`forecast_frozen`, `up_to_date`,
`waiting_for_data`, `failed`, `locked`, `config_error`, `no_upcoming_week`), season, target week and
its first kickoff (UTC and Eastern), stages with timings, `nfl_synced_at` (last successful nflverse
sync), latest final week, readiness warnings, previous-week finality, last included training week,
artifact ids and whether they were created or reused, frozen projection counts, evaluation
progress (prospective weeks, readiness per position), the target week's pregame ESPN snapshot
status (read-only), the missed window, errors and required actions. Sync output goes to the log
file only; no environment variable, token or cookie is written anywhere.

**Kickoff safety.** Training and prediction are refused once the target week has kicked off (checked
again at predict time). A week whose first kickoff passes without any frozen pregame row is
recorded write-once in `<out>/missed/s<season>w<week>.json`; nothing is backdated and no
retrospective replay is substituted (a manual `--retrospective` replay is still possible and is
never pregame evidence).

**Idempotence and restarts.** Artifacts are content-addressed and ledger runs dedupe on their inputs
hash, so a repeated cycle changes nothing and reports `up_to_date`. Artifacts, ledger runs and missed
records are written to a temporary file and hard-linked into place, so an interrupted write never
leaves a partial file; a truncated file from an older version is reported (`NotReady`) and never
overwritten. A changed input before kickoff (a stat correction, a late injury report) produces a new
artifact and a new ledger run; every original stays byte-identical.

**Limitations.** A Mac that is shut down skips the attempt (asleep: it runs on wake); three attempts
Tue-Thu morning cover most delays, but if nflverse publishes after the Thursday attempt and before a
Thursday-night kickoff, run `learning_cycle.py run` by hand. If the refresh and the cycle start
together, the cycle's sync fails on DuckDB's write lock and the next attempt retries. The
participation artifact is not retrained (unchanged); the challenger stays shadow-only.

### Pregame source provenance (`app/analytics/source_provenance.py`)

**Audit: what could be silently rewritten.**

| Source | Storage | Rewritten by later refreshes? |
|---|---|---|
| nflverse injury reports (`nfl_injuries`) | live DB, season partition | **Yes**: every `sync_nfl.py` replaces the season. `load_injuries` drops rows with `date_modified` at or after kickoff, but rows without `date_modified` (many 2025-2026 rows) cannot be filtered by time |
| nflverse weekly rosters, schedules, box scores, snaps, expected points | live DB, season partitions | **Yes**, replaced on every sync (stat corrections, flexed kickoffs, roster status changes) |
| Play-by-play injury evidence (`pbp_exits/<season>.parquet`) | weekly model dir | **Yes**, rewritten by `sync-exits` |
| nflverse depth charts (`nfl_depth_charts`) | live DB | Yes, but no forecast reads it (participation ranks depth from snaps) |
| ESPN status fallback (`fantasy_players.injury_status`) | live DB | **Yes**, every ESPN sync |
| ESPN projection snapshots (`espn_projection_history`) | live DB, one row per player, week and **local date** | A same-day sync replaced that day's row, including one captured before kickoff by a sync after it. **Fixed**: such a row is now locked (below) |
| Model artifacts, ledger runs | weekly model dir | No: content-addressed / write-once (earlier sections) |
| Production `ifp-1.0.0` runs (`independent_projection_runs`) | live DB | `project` replaces the week's rows (unchanged; documented above) |

No historical leakage is claimed: forecasts were made pregame from the tables as they were then.
The problem was reconstruction: a past forecast's inputs could only be re-read from tables that had
since been replaced.

**What is captured.** Every `weekly_learning.py predict` (and so every learning cycle) snapshots the
exact `Sources` frames the forecast reads (box-score panel, schedule, defense, weekly rosters,
injury reports, play-by-play exits) for the target season and the previous one, which is every
season the feature pipeline reads (a test rebuilds a forecast from these two seasons against a
database holding three). When the ESPN-status fallback supplied a team's designations, the week's
designations as P(appears) used them are captured too (gsis id, team, status; no ESPN ids, cookies
or raw payloads). Each part is a parquet file named by the sha256 of its content
(`<out>/snapshots/<family>/s<season>/<sha>.parquet`), so an unchanged season (e.g. the previous one)
is stored once. A write-once manifest per capture
(`<out>/snapshots/manifests/s<season>w<week>/<id>.json`) lists the parts, and the ledger run stores
the manifest id and part hashes as part of its inputs (a changed source gives a new run). If the
snapshot cannot be written, no forecast is frozen. `weekly_learning.rebuild_run` recomputes a run
from its snapshots and artifacts only and must reproduce its rows and inputs hash.

**Timestamps.** Event = the NFL week and the team's kickoff. Published = nflverse `date_modified`
per injury row, when present; its coverage per season (and the count of rows without a player id)
is recorded as `available`, `partial` or `missing` and never imputed. Retrieved = an upper bound on
the download time: the finish of the nflverse sync that wrote the live season (`sync_runs`), the
play-by-play file's write time, or null (history seasons). Captured = the forecast's creation time.
Used = the ledger run's `created_at`. `known_by` = retrieved if known, else captured.

**Point-in-time reads.** `source_provenance.injuries_as_of(season, week, kickoffs)` returns, per team,
the week's injury rows from the latest snapshot known strictly before **that team's** kickoff,
reading snapshots only. An update received after kickoff never reaches that team; a team with no
snapshot before its kickoff gets no rows (listed in `no_pregame_snapshot`), never today's table.

**Corrections.** A corrected source (a stat correction, a re-issued injury report) is a new part and a
new manifest; earlier parts, manifests, artifacts and ledger runs stay byte-identical, and every
earlier forecast still rebuilds exactly. Unreadable or hash-mismatched snapshots raise
`ProvenanceError`; nothing falls back to the current database.

**Injury evidence quality.** Each ledger row carries `injury_evidence` for the player's last game
before the forecast, from the forecast's own game classification (descriptive, not a model input):
`pbp_injury_mention` (play-by-play says he was injured in that game), `injury_report_listing` (snap
drop plus a new report listing after it, with the report's publication-time status),
`snap_drop_without_injury_evidence` (role cut, rest, game script or an unreported injury: never
called an injury), `no_snap_drop` or `unknown`, plus whether play-by-play covered that week.

**ESPN pregame benchmark.** `sync_espn.py` keeps one history row per player, week and day. A row
captured before the player's team kicked off is now locked once that kickoff has passed: a later
same-day sync neither deletes nor overwrites it (`espn.sync.pregame_history_locked`). Players whose
game has not started are replaced as before; with an unknown team or kickoff nothing is locked.

**Retention and deployment.** Snapshots live in the git-ignored weekly model directory on the machine
that runs the cycle; they survive process restarts and database rebuilds (separate files), are not
published to MotherDuck, and are lost with that disk, so include `data/models/` in backups. Size:
two seasons of frames per new capture, a few MB at most, deduplicated by content; never delete a part
a manifest references.

**What remains unrecoverable.** Forecasts made before this capture existed have no snapshot and
`rebuild_run` refuses them. Seasons synced before now carry only today's version of their injury
reports, rosters and stats: their original publication or retrieval times cannot be recovered, and
`date_modified` is missing for many 2025-2026 injury rows. Retrospective replays of past weeks therefore
use present-day tables and stay labeled retrospective. Sources that should adopt the same snapshot
store next: the ESPN projection snapshots themselves (today a DB table), nflverse depth charts if a
model starts reading them, and any future injury or news feed.

### Prospective evaluation: outcome finality

`evaluate` labels every frozen forecast row with an explicit outcome status
(`weekly_learning.label_outcomes`). Finality is decided **per player's own game**, never per week:
a final Thursday or Sunday game says nothing about a Monday game. nflverse schedules have no
game-status field and do not say whether a score can be live, so a game is **presumed final** only
under the rule `schedule_intelligence` already uses: both scores present AND the schedule fetched at
least 8 h after kickoff (after a 1 h download margin), the fetch time being the start of the latest
successful nflverse sync that wrote the season's schedule (`sync_runs`). An unknown fetch time never
presumes a game final. The same rule gates training readiness (`weekly_learning.game_presumed_final`).

| Status | Rule | Conditional | Unconditional |
|---|---|---|---|
| `appeared` | his team's game is presumed final, the team's box scores AND snap counts for the week are synced, and he has a panel row (box score or offensive snaps > 0) | league points | league points |
| `confirmed_absent` | same finality and completeness, no panel row (inactive, healthy scratch, did not play) | excluded | **0** |
| `pending_game` | his team's game is not presumed final (no score, or a score that may be live) | excluded | excluded |
| `missing_ingestion` | game final but the team's box scores or snaps are not synced yet, or the game the forecast saw (row kickoff) is missing from the current schedule | excluded | excluded |
| `bye` | no game for his team, neither when the forecast was made nor now | excluded | excluded |
| `unresolved_identity` | no `gsis_id`, an id on no roster or box score of the season, or his week box score / roster row is for a different team than the forecast | excluded | excluded |

Only `appeared` and `confirmed_absent` carry a label; every other status has a null label, so an
unfinished, unsynced or unmatched player can never become a zero-point outcome (asserted).

A week is **prospective evidence** only when it is fully final: every scheduled game is presumed
final, every scheduled team's box scores and snaps are synced, and every game the frozen forecasts saw
is still in the schedule (an incomplete schedule download never turns teams into byes). `weeks`, the per-position
metrics, the bootstrap CI, pair decisions and `shadow_status` use fully final weeks only, so a
partly final week counts as **zero** prospective weeks toward `SHADOW_PROMOTION_CRITERIA`.
Resolved players of partly final weeks are reported separately under `preliminary` (marked
PRELIMINARY, no bootstrap, never read by `shadow_status`). The report also carries `finality` per
week (scheduled / final / complete teams, pending and missing-ingestion teams) and `outcomes`
(status counts for evidence and not-yet-evidence rows). Rerunning `evaluate` after the last game
is final and synced turns the week into evidence; the ledger is never rewritten.

### Game classification and healthy-game usage (all point-in-time)

Every pre-cutoff game is classified for a forecast of week W (`forecast_features.classify_games`):

| Class | Rule (snap drop = snap share >= 15 points below the mean of the player's previous <= 4 games) |
|---|---|
| `injury_exit` | snap drop AND (play-by-play says he was injured in that game, OR a NEW injury listing appears within 2 weeks, using reports up to W's pregame report only) |
| `blowout_low` | snap drop, no injury evidence, final margin >= 17 |
| `listed_low` | snap drop, listed after it but also on that week's own report (chronic listing; ambiguous) |
| `role_low` | snap drop with no injury evidence (possible role cut, coaching decision, rest, or an unreported injury; ambiguous) |
| `full` / `unknown` | no snap drop / no snap share or reference |

Low fantasy points never mark an injury. Play-by-play evidence (`app/nfl/pbp_exits.py`) comes from
"<TEAM>-<jersey>-<name> was injured during the play" in nflverse play descriptions, mapped with that
week's roster (team + jersey) and confirmed by surname; unconfirmed mentions are dropped.

Measured on 2020-2026 week 5 (`challenger_research.py exits-audit`): 1,283 `injury_exit` games (435
with a play-by-play mention, 848 report-only). The ifp-1.0.0-era `injury_exit_last` label agrees on
862 of its 1,265 forecast rows; only ~20% of its labeled exits have a play-by-play injury mention.
A play-by-play mention alone does not mean an exit (median snap share 85% of normal). Next
appearance after each class (players with reference snap share >= 40%, median opportunities vs the
pre-game reference): RB 0.86 after a play-by-play exit vs 0.58 after `role_low`; WR 0.83 vs 0.52;
only ~25% of play-by-play exits appear the following week.

Challenger inputs (all from games before W; `FEATURE_DOCS` has exact definitions):

* `h_*`: usage over the last <= 5 games that were not injury exits; `inj_exits_recent`,
  `last_injury_exit`, `last_role_low`, `last_blowout_low`.
* `prev_*`: previous-season usage over healthy games (`prev_xfpl_pg` shrunk 2 games toward the
  position mean); `prev_team_changed`.
* `xfpl_eb` (established workload): this season's healthy-game expected league points per game shrunk
  toward `prev_xfpl_pg` by k = 3 games (toward the position mean with no previous season). The weight
  on the current season is n / (n + 3) after n healthy games: 25% after 1, 50% after 3, 77% after 10.
  On development, k in {~0, 1, 3, 6, 10} were indistinguishable (pooled MAE 4.208-4.211); k = 3 is the
  pre-set default.
* Expected league points (`xfpl*`): nflverse expected counts x this league's weights, split into
  touchdown and non-touchdown parts. For this league (full PPR, 4-point passing TD) it is nearly
  identical to nflverse xFP (correlation 0.999998); it matters for other scoring formats.

### Model selection (frozen before confirmation, commit `cc0845b`)

Windows: development 2021-22 (all choices), confirmation 2023-24 (one run), exploratory 2025 + 2026
weeks 1-5 (already inspected; never used to choose), prospective 2026 week 6 onward.

| Candidate (development, pooled MAE; C = 4.215) | Result | Decision |
|---|---|---|
| Huber loss (c = 1.0 / 1.345 / 2.0) | 4.052 / 4.080 / 4.126, but bias +0.63 / +0.47 / +0.22 (C -0.23), RMSE worse, pair accuracy lower | rejected: lowers MAE by targeting the median, not a better expectation |
| Touchdown/yardage efficiency split (X) | 4.211, after-TD-boom cohort worse | rejected |
| Team volume / margin context (K) | 4.216 | rejected |
| Healthy-usage averages added (U) or replacing raw usage (R) | 4.212 / 4.209; after-injury-exit cohort worse | rejected |
| Previous-season usage (P, Q) | 4.216 / 4.212; team-change cohort -1.2% | Q kept |
| Game-class flags (F) | 4.214; after-role-drop cohort -2.9% | kept |
| Established workload blend (E) | 4.208 [-0.01, -0.00] | kept |
| Recency half-life 18 / 36 / 72 weeks on C | 4.207 / 4.210 / 4.212 | 36 weeks kept with F+E+Q |
| **F + E + Q, 36-week half-life (RB/WR/TE)** | **4.203 [-0.018, -0.004]**, RMSE 5.869 vs 5.878 | **frozen**; QB keeps C (no addition helped QB) |

### Confirmation (2023-24, run once) and exploratory replay

| Pos | n | A1 | A2 | B | C | ifp-2 | ifp-2 - C (95% CI) | RMSE C / ifp-2 | Bias C / ifp-2 |
|---|---|---|---|---|---|---|---|---|---|
| QB | 1,330 | 6.30 | 6.19 | 6.29 | 6.056 | 6.056 | 0 (same model) | 7.60 / 7.60 | +0.17 / +0.17 |
| RB | 3,398 | 4.08 | 4.07 | 4.16 | 4.015 | 4.006 | -0.009 [-0.022, +0.004] | 5.67 / 5.68 | -0.06 / -0.02 |
| WR | 5,409 | 4.39 | 4.29 | 4.36 | 4.237 | 4.211 | -0.026 [-0.038, -0.014] | 5.97 / 5.96 | -0.05 / +0.03 |
| TE | 3,208 | 2.95 | 2.89 | 2.95 | 2.860 | 2.853 | -0.007 [-0.024, +0.009] | 4.23 / 4.23 | +0.08 / +0.09 |
| All | 13,345 | 4.16 | 4.09 | 4.16 | 4.031 | 4.016 | -0.014 [-0.023, -0.007] | 5.72 / 5.72 | +0.00 / +0.05 |

All `CONFIRMATION_RULES` passed -> status `shadow`. Start/Sit pairs 65.45% -> 65.57% (regret 2.505
-> 2.495), close calls 60.38% -> 60.52%. Exploratory 2025-26 (n = 8,645, not a fresh test): pooled
-0.018 [-0.028, -0.009], RB -0.030 [-0.049, -0.010], WR -0.020 [-0.034, -0.008].

Where it helps and where it hurts (confirmation; exploratory agrees in direction):

| Cohort | n | C | ifp-2 | Bias C / ifp-2 |
|---|---|---|---|---|
| After a role drop (`role_low`) | 1,016 | 3.29 | 3.18 | -0.61 / -0.29 |
| Team changed | 2,957 | 3.70 | 3.62 | -0.32 / -0.09 |
| Starters pool | 5,616 | 5.76 | 5.74 | +0.24 / +0.28 |
| After an injury exit | 292 | 4.40 | 4.42 | +0.47 / +0.15 |
| Repeated injury exits (>= 2 of last 5) | 179 | 3.75 | 3.77 | -0.28 / -0.46 |
| After a touchdown boom | 303 | 6.15 | 6.10 | +1.03 / +1.04 |

After injury exits the challenger removes most of C's under-projection but is no more accurate:
returning players get ~85% of their previous opportunities and some exit again, so restoring the
full established workload overshoots about as often as C undershoots. Exploratory: 4.41 -> 4.50.

### Workload vs availability

The data does not support a second probability model for "normal workload given he appears": an
injury exit happens in ~2.7% of appearances and prior exits do not raise it (QB 2.8% -> 4.7%, RB 2.7%
-> 3.6%, WR 3.3% -> 3.8%, TE 2.6% -> 2.8% after one recent exit; lower after two). The real
availability risk after an exit is missing the next game, which `participation.py` already models
(it sees `injury_exit_last` and the week-W report). The conditional estimate keeps
`inj_exits_recent` as an input, so partial-game risk stays in the conditional mean rather than being
assumed away.

### Intervals

The challenger reuses the empirical tercile bands, built from development-window residuals (no
multipliers). Coverage is flat across volatility terciles (e.g. WR 79-85%), so no volatility split
was added. QB coverage 74.0% (confirmation) is outside 75-85%: QB intervals are withheld, as for C.
The band's high end is the 90th-percentile outcome (the "ceiling").

### Promotion

`PROMOTION_CRITERIA` and the participation criteria are unchanged. The challenger additionally
needs `challenger.SHADOW_PROMOTION_CRITERIA` on frozen pregame forecasts: >= 8 prospective weeks,
>= 400 matched appearances per position, challenger - champion MAE CI entirely below 0, bias not
worse by > 0.15, starters-pool MAE not worse, interval coverage 75-85%. Meeting it yields
`eligible_for_review`, never a promotion; consumer-facing use also needs the existing
`PROMOTION_CRITERIA` (>= 3% vs the best trailing baseline, ESPN rule) and your approval. The
challenger is expected to fail the 3% bar: its confirmed gain is ~0.4%.

**Fail-closed enforcement (`weekly_learning.shadow_status`).** Every condition is evaluated per
position as `{name, observed, required, result, explanation}` with `result` in `pass`, `fail` or
`insufficient_data`. Missing evidence is `insufficient_data`, **never a pass**. Thresholds come
from `challenger.SHADOW_GATE`, which restates `SHADOW_PROMOTION_CRITERIA` (tested to agree; the
pre-registered text is unchanged so challenger artifact ids are unchanged).

| Criterion | Required | `insufficient_data` when | `fail` when |
|---|---|---|---|
| `frozen_pregame_evidence` | report kind `pregame` | - | retrospective (or any other) replays |
| `prospective_weeks` | >= 8 fully final weeks | fewer | - |
| `matched_appearances` | >= 400 per position (player appeared, both models forecast) | fewer | - |
| `mae_vs_champion` | week-clustered 95% CI upper bound of challenger - champion MAE < 0, identical rows | no CI (< 2 weeks of paired rows) | CI upper >= 0 |
| `bias_guard` | \|challenger bias\| <= \|champion bias\| + 0.15 | either bias missing | worse by more than 0.15 |
| `starters_pool_mae` | challenger MAE <= champion MAE on the starters pool | pool empty or a MAE missing | challenger worse |
| `interval_coverage` | challenger 80% coverage in [0.75, 0.85] | no challenger intervals | under- or overcoverage |

The **starters pool** is one definition everywhere (`forecast_backtest.starters_pool_expr`): per
(season, week, position) the top `PAIR_POOL` players (QB 24, RB 48, WR 60, TE 24) by the
recent-average baseline A1, a pregame quantity identical for every model. The same pool drives the
Start/Sit pair decisions and the challenger research cohorts. In the prospective evaluation the rank
runs over **every frozen pregame forecast** of the week, before anyone knows who appears; MAE is then
measured on the pool members who appeared, so an inactive top player leaves a gap instead of being
backfilled. (Backtest stores hold appearances only, so there the same rank runs over appearing players.)
QB challenger intervals are withheld
by policy (confirmation coverage 74.0%), so QB `interval_coverage` is `insufficient_data` with that
explanation: QB cannot reach `eligible_for_review` until coverage evidence exists. This is a
reported limitation, not an exemption.

Status vocabulary (separate axes):

| Field | Values | Meaning |
|---|---|---|
| `status` (readiness) | `insufficient_evidence` | a sample criterion (pregame evidence, weeks, appearances) has not passed, or any criterion lacks evidence |
| | `failed_evaluation` | enough evidence and at least one criterion fails |
| | `eligible_for_review` | every criterion passes; the most an evaluation can produce |
| `lifecycle` | `research_shadow` | ifp-2.0.0 in every position: never read by lineups, Start/Sit, trades, waivers, matchups or the website |
| | `approved_production` | only by explicit user approval plus a reviewed code change that wires a model into consumers; never set by evaluation |
| `production_gate` | `not_evaluated` | `PROMOTION_CRITERIA` stays a separate gate the weekly evaluation does not evaluate |

`evaluate` writes `shadow_status` (per position: `status`, `lifecycle`, `criteria`, `blocking`,
`production_gate`) and `promotion_blockers`, which answers "what prevents ifp-2 from replacing
ifp-1?" per position: every non-passing criterion's explanation plus the approval and production
gate. The CLI summary prints one line per criterion.

### Opportunity + efficiency decomposition (research, negative)

`docs/research/opportunity_efficiency.md`: forecasting realized expected league points and points
minus xFP with separate ridges (`ifp-oe-0.1.0`, research only) equals the direct ridge when features,
penalty and rows match, and no separate penalty or feature split beats ifp-2 on the 2023-26 replay
(efficiency residual out-of-sample R^2 <= 0.007). Absent nflverse xFP for a zero-opportunity
appearance in a covered team-week is a measured zero; treating it as missing biases any xFP-target
model by about -0.9 points.

### Model registry (approval manifest, research only)

`docs/model_registry.md`: `backend/app/analytics/model_registry.json` records the approved
production model (`ifp-1.0.0`, unchanged, an `incumbent` record, not an owner approval) and every
other model's lifecycle state (`ifp-2.0.0` prospective shadow, `ifp-hgb-0.1.0` experimental).
`scripts/model_registry.py status` is a dry-run promotion gate: artifact sha256 integrity, scoring
fingerprint, finalized pregame prospective evidence recomputed per position, `PROMOTION_CRITERIA`,
the ESPN rule where claimed, a restorable rollback target and an explicit owner approval. It never
changes a consumer; the hosted app does not import it.

### Runtime

Feature store 2020-2026 (42.7k rows, with classification) 6.4 s; development grid of 20 specs x 8
penalties 16 s (parallel); confirmation 2.8 s; weekly `train` ~10 s; `predict` ~1 s; play-by-play
sync ~0.5 s per season (nflverse cache). Artifacts ~30 KB each, ledger runs ~0.7 MB per week (436 players in week 6).

## Known biases and unsupported populations

* **`projected_points` is still conditional on playing**; use `expected_points` for an
  unconditional number. Participation is `experimental` for QB and RB (calibration only).
* **Participation has no team-level coherence.** Each player is scored alone, so two backup QBs on
  one team can both get ~0.74 (CHI, week 6) although only one usually plays. QB miscalibration in
  the 0.3-0.6 range is consistent with this.
* **Live designations may be from ESPN** (see "Participation in live runs"); that mapping is not
  validated. Practice status is unknown in that case.
* **Injury-exit labeling is partial.** `injury_exit_last` needs an injury listing within two weeks;
  rest listings are excluded, but unrelated listings still count and blowout snap drops look like
  role drops. It changed the conditional model's validation MAE by nothing measurable, so
  `ifp-1.0.0` keeps `snap_trend`; the week-6 Barkley / Rice / Moore gaps to ESPN are therefore
  unchanged in `projected_points` (Barkley and Moore are now flagged as injury exits and their
  P(appears) is 0.77 / 0.52; Rice is flagged as a possible demotion).
* Low-volume RBs and players with < 3 recent games are worse than simple averages.
* ifp-2 challenger: the injury-exit classification is partial (report-only exits are noisier than
  play-by-play ones; week-18 rest and unreported injuries look like role cuts), and after injury exits
  it is no more accurate than C (see "Where it helps and where it hurts").
* Team changes carry old-team shares (flagged); rookies abstain until their first game.
* The conditional model uses no injury reports, depth charts, coaching or QB changes (the
  participation model uses injury reports and a snap-based depth order); no weather or Vegas.
* Label noise: one 2026 WR player-week differs from ESPN by 2.0 (unexplained).
* K and D/ST are not modeled.

## Recommended next steps

0. Run the weekly learning cycle every week (before Thursday's kickoff) so the challenger accumulates
   frozen pregame forecasts; check `weekly_learning.py evaluate` after 8 weeks.
1. Follow the weekly evidence routine above so `espn_projection_history` accumulates true pregame
   snapshots and every week has a stored run; rerun `evaluate` once the `/data` tracker shows
   >= 300 matched completed player-weeks per position.
2. Recalibrate QB/RB participation (e.g. isotonic or Platt on validation predictions) and add a
   team-level constraint for QBs (probabilities of a team's QBs taking the first snap sum to 1),
   then re-evaluate on a later held-out window; the 2025-26 held-out has now been used.
3. Sync the nflverse injury report daily from Wednesday so live runs rarely need the ESPN-status
   fallback, and measure the fallback against nflverse designations once both exist for a week.
4. A finer injury-exit signal (per-quarter snaps from play-by-play) could sharpen
   `injury_exit_last`.
5. Only then let the Start/Sit page show both lineups, with the model statuses visible.

# ifp-2.2.0: research shadows promoted into Ours (owner override) and the backtest guard

**Decision (owner, 2026-10-19).** "Force-promote shadows now: owner-override the prospective shadows into Ours today
without waiting for evidence." Production becomes `ifp-2.2.0` = `ifp-2.1.0` plus the frozen specs of the prospective
research shadows. It is recorded in the registry as an `owner_approval` with `override: true`. The shadows'
pre-registered prospective gates (at least 8 fully final weeks from 2026 week 7, at least 400 matched appearances per
position) are not met, and nothing here pretends otherwise. The shadows keep freezing their weekly ledgers unchanged;
those ledgers are the post-hoc evidence.

**Engineering safeguard (owner-approved).** Before shipping, the combined model is backtested against `ifp-2.1.0` on
identical player-weeks. A component that makes a position clearly worse (week-clustered 95% MAE CI entirely above 0)
is left out for that position. Every neutral or better component ships.

Code: `backend/app/analytics/context_model.py` (the model), `backend/app/analytics/weekly_learning.py`
(`train_context`), `scripts/ifp22_backtest.py` (the guard), tests in `backend/tests/test_context_model.py`.

## What ships, per position

| Position | Components shipped | Left out |
|---|---|---|
| QB | `qb_scenarios` (ifp-ctx-p2q), `depth_tier` (ifp-dct-p1) | `qb_participation` (ipm-cal-0.1.0): clearly worse on unconditional MAE |
| RB | `qb_context`, `teammates` (ifp-ctx-p1), `depth_tier` (ifp-dct-p1), `decline_role` (ifp-awd-p1) | none |
| WR | `qb_context`, `teammates` (ifp-ctx-p1), `depth_tier` (ifp-dct-p1), `decline_role` (ifp-awd-p1) | none |
| TE | `decline_role` (ifp-awd-p1) | none |

Not a candidate: **`ifp-fr-p1` (FTN first reads)**. FTN charting is CC-BY-SA 4.0 ("FTN Data via nflverse").
Serving a projection derived from it may carry attribution and ShareAlike obligations the owner has not decided, so
it is excluded from served numbers. Its shadow keeps running. This is pending the owner's licensing call
(`context_model.EXCLUDED`).

Each component is its shadow's own frozen definition, with no refit choices:

* **`qb_scenarios`** (ifp-ctx-p2q, `qb_scenario_mixture.md` section 8). Start candidates and the conditional-logit
  start model `qbstart-0.1.0`, refitted every week on team-weeks strictly before the forecast week. QB features
  `s_start` (the realized start in training) and `qb_self_epa`. The projection is the mixture
  `pi x f(start) + (1 - pi) x f(no start)`, with `pi = P(start | appears)`. A team-week without a start candidate
  uses a fallback QB model fitted without the scenario group. The code is the shadow's file
  `scripts/qb_scenarios.py`, which is sha256-bound into the p2q spec, imported as is. Its sha256 is recorded in every
  artifact.
* **`qb_context`** and **`teammates`** (ifp-ctx-p1, `qb_teammate_context.md` section 8). The receiver groups
  `context_challenger.GROUPS["qb"]` and `["tm"]` for RB and WR (p1's spec "C"), from
  `roster_context.context_for_week`. TE keeps no p1 group, as in p1.
* **`depth_tier`** (ifp-dct-p1, `signal_shadows.md`). `dc_tier` from nflverse modern depth charts (2025 on). The
  team's latest snapshot strictly before kickoff minus 1.5 h and before the run, at most 8 days old. Production keeps
  its own content-addressed snapshots in `<weekly-dir>/depth_raw` (`weekly_learning.py sync-depth`, which the learning
  cycle runs after `sync-exits`), never the shadow's directory. A missing snapshot goes through the ridge's missing
  indicator, as in the shadow.
* **`decline_role`** (ifp-awd-p1). `xfpl_eb` is replaced by `aw_role_d`
  (`adaptive_workload.DECLINE_CONFIG`) for RB, WR and TE.

## Overlaps, resolved once

* **ifp-ctx-p1 vs ifp-ctx-p2q at QB.** Both change the QB model: p1's group A (`qb_is_expected`, `qb_self_epa`)
  against p2q's scenario mixture. p2q was pre-registered after p1 as its QB successor, and it has the better
  development result: QB MAE -0.264 [-0.383, -0.149] vs p1's -0.146 [-0.273, -0.003] on 2021-22. **QB uses p2q.** p1
  contributes only its RB/WR groups.
* **ifp-ctx-p1's teammate group vs ifp-2.1.0's injury handoff (RB/WR).** Both describe unavailable teammates. They
  are not alternatives for the same slot: the handoff is part of the base and stays, and p1's group is added on top.
  The guard then measures the group's marginal effect *with* the handoff present: WR -0.012 [-0.019, -0.005], RB
  +0.001 [-0.007, +0.008]. It ships at both positions.
* **`depth_tier` and `qb_scenarios` at QB.** These are separate features. In the mixture, `dc_tier` keeps its
  observed value in both scenarios. Measured as a leave-one-out, depth is -0.015 [-0.038, +0.011] at QB.

## Backtest guard

`scripts/ifp22_backtest.py`, rule fixed before the run (its docstring).

* **Walk-forward, exactly as production trains.** `context_model.fit` uses ifp-2.1.0's training rows, frozen
  penalties and 36-week recency, refitted every week on strictly earlier weeks. The QB start model is refitted the
  same way.
* **Data.** A scratch copy of the live and history databases, taken 2026-10-19 (never the launchd database).
* **Evaluation window.** 2023 week 1 to 2026 week 6: 59 fully final weeks.
* **Comparison.** Identical player-weeks: every compared model forecast the row and the label exists. Conditional
  points, MAE, week-clustered bootstrap (2,000 draws).
* **Variants.** For each component: leave one out (`full` minus `drop_c`; this decides) and add only it (`only_c` minus
  `b21`; reported).

### ifp-2.2.0 (combined) minus ifp-2.1.0

| Position | n | ifp-2.1.0 MAE | ifp-2.2.0 MAE | Difference [95% CI] | RMSE | Bias (2.1 -> 2.2) |
|---|---|---|---|---|---|---|
| QB | 2,164 | 6.247 | 5.880 | **-0.367 [-0.444, -0.276]** | 7.812 -> 7.465 | +0.074 -> -0.103 |
| RB | 5,594 | 3.976 | 3.951 | **-0.025 [-0.051, -0.003]** | 5.697 -> 5.696 | -0.031 -> -0.019 |
| WR | 8,886 | 4.109 | 4.078 | **-0.030 [-0.043, -0.018]** | 5.828 -> 5.819 | +0.003 -> +0.048 |
| TE | 5,346 | 2.906 | 2.906 | +0.000 [-0.002, +0.002] | 4.353 -> 4.353 | +0.071 -> +0.070 |

### Per component: leave-one-out (decides) and single addition (reported)

| Position | Component | full - drop_c [95% CI] | only_c - ifp-2.1.0 [95% CI] | Guard |
|---|---|---|---|---|
| QB | qb_scenarios | -0.298 [-0.388, -0.207] | -0.352 [-0.433, -0.263] | ships |
| QB | depth_tier | -0.015 [-0.038, +0.011] | -0.069 [-0.122, -0.022] | ships |
| RB | qb_context | +0.001 [-0.006, +0.009] | +0.001 [-0.007, +0.009] | ships (neutral) |
| RB | teammates | +0.001 [-0.007, +0.008] | +0.003 [-0.005, +0.010] | ships (neutral) |
| RB | depth_tier | -0.032 [-0.054, -0.014] | -0.030 [-0.051, -0.012] | ships |
| RB | decline_role | +0.002 [-0.001, +0.004] | +0.003 [+0.001, +0.006] | ships (neutral in the combined model) |
| WR | qb_context | -0.001 [-0.007, +0.006] | +0.001 [-0.005, +0.007] | ships (neutral) |
| WR | teammates | -0.012 [-0.019, -0.005] | -0.007 [-0.015, +0.001] | ships |
| WR | depth_tier | -0.024 [-0.033, -0.016] | -0.020 [-0.027, -0.013] | ships |
| WR | decline_role | +0.000 [-0.001, +0.002] | +0.000 [-0.001, +0.002] | ships (neutral) |
| TE | decline_role | +0.000 [-0.002, +0.002] | (only component) | ships (neutral) |

No points component's leave-one-out CI lies entirely above 0, so nothing was dropped on points and no second pass was
needed. Two numbers point slightly the wrong way, and they are the first thing to watch prospectively:

* **RB `decline_role`.** Added alone to ifp-2.1.0 it is +0.003 [+0.001, +0.006]. Inside the combined model (what
  ships) it is +0.002 [-0.001, +0.004].
* **WR `decline_role` in development** (2021-22, reported only). Its leave-one-out is +0.002 [+0.000, +0.005].

`depth_tier` has data from 2025 only (modern charts), so its whole effect comes from the 2025-26 rows. In development
it is exactly 0, because every row there takes the missing-indicator path.

### QB P(plays): `qb_participation` (ipm-cal-0.1.0), left out

Population: every QB the participation model forecast in the window, whether he played or not (4,038 QB-weeks, 54%
played).

* **Inputs.** The participation model's walk-forward out-of-sample P(plays), from `participation_calibration.py run`
  on the same scratch copy. The calibrated P is the study's own expanding-window isotonic map, refitted every week.
* **Points.** ifp-2.2.0's conditional QB points where the player appeared; for the rest, the study's frozen
  conditional points (`cond`). Actual points are 0 for a QB who did not play.

| | Base P | Calibrated P |
|---|---|---|
| Brier | 0.1177 | 0.1152 |
| Log loss | 0.371 | 0.362 |
| Unconditional MAE | 4.088 | 4.104 |
| Unconditional RMSE | 6.130 | 6.133 |

Unconditional MAE, calibrated minus base: **+0.016 [+0.003, +0.031]**, entirely above 0, so **left out for QB**. The
probabilities do get more honest, but the expected points get worse, which is what the study itself found
(`participation_calibration.md`: "it does not yet make QB projections more accurate"). With the study's own
conditional points the difference is +0.010 [-0.004, +0.024]. The ipm-cal shadow keeps scoring the frozen map
prospectively. The code path (`context_model.calibrate_rows`) stays and is tested, so a later owner decision only
has to move the component back into `SHIPPED`.

### Development window (2021-22, reported only)

| Position | ifp-2.2.0 - ifp-2.1.0 [95% CI] | Bias (2.1 -> 2.2) |
|---|---|---|
| QB | -0.264 [-0.383, -0.150] | -0.525 -> -0.710 |
| RB | -0.003 [-0.015, +0.009] | -0.064 -> -0.079 |
| WR | -0.001 [-0.011, +0.011] | -0.285 -> -0.285 |
| TE | +0.002 [-0.001, +0.004] | -0.053 -> -0.054 |

The QB row reproduces the p2q study's development numbers exactly: -0.264 [-0.383, -0.149] and bias -0.710
(`qb_scenario_mixture.md` section 4). That checks that production calls the shadow's model unchanged. It also shows
the known p2q starter-level bias in 2021-22. In the evaluation window that bias is -0.103.

## Live behavior

Every component reads only what is known at the cutoff week W.

* **Horizon 0.** Uses W's injury report, with the ESPN status fallback for teams without a synced nflverse report,
  as P(plays) does.
* **Later target weeks.** The same as-of-W context, with W's next-game designations dropped: for example, a starter
  Out at W is no longer treated as out at W+3.
* **Depth tier.** Taken for W's game from snapshots made before the run.

ifp-2.1.0's handoff and return-rate P(plays) are unchanged inside ifp-2.2.0.

Weekly training cost: about 1.6 s per training week for the component features (handoff, context, role, start
candidates), so a few minutes for the ~112 training weeks.

## Reproduce (scratch copies only, from `backend/`)

```
export FANTASY_DB_PATH=<scratch>/fantasy.duckdb NFL_HISTORY_DB_PATH=<scratch>/nfl_history.duckdb \
       WEEKLY_MODEL_DIR=<scratch>/models/weekly IFP_MODEL_DIR=<scratch>/models INDEPENDENT_MODEL_DIR=<scratch>/models
uv run python ../scripts/weekly_learning.py sync-depth
uv run python ../scripts/ifp22_backtest.py build                 # ~10 min on the full history
uv run python ../scripts/ifp22_backtest.py run --development     # ~1.5 min
uv run python ../scripts/participation_calibration.py run --live-db $FANTASY_DB_PATH \
       --history-db $NFL_HISTORY_DB_PATH --models $IFP_MODEL_DIR  # QB P(plays) inputs, ~6 min
uv run python ../scripts/ifp22_backtest.py participation
```

The report is written to `<scratch>/models/research/ifp22/report.json`.

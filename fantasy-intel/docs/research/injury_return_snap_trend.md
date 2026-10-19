# Injury return vs `snap_trend` (ifp-2.2.0): no-gain candidate, dropped

**Question (owner, 2026-10-19).** ifp-2.2.0 feeds the raw last-game `snap_trend` to its ridge even when that game
was an injury exit (`injury_exit_last = 1`). Saquon Barkley (00-0034844) hurt his hamstring on his first carry in
2026 week 4, so `snap_trend = -0.53` costs him **-2.6 conditional points** in every rest-of-season week. Ours has
him at about 7-9 conditional points, although the workload handoff gives him 59% of PHI carries. Does a narrow fix
for players *returning* from an injury exit make the projections more accurate?

**Answer: no. The candidate is clearly worse on the cohort it targets, so it is dropped and production is
unchanged.** For a player returning from an injury-exit absence, the `snap_trend` penalty mostly acts as a
"first games back" discount, and on average that discount is warranted. RBs in this cohort are already
*over*-projected by ifp-2.2.0: +0.79 points at horizon 0. Lead RBs (pre-exit healthy snap share >= 60%) are
over-projected by +1.12. Removing the penalty makes this error larger.

Code: `scripts/injury_return_research.py` (`horizons`, `run`, `example`). Outputs are written to
`<ifp-dir>/research/injury_return/` (git-ignored).

## Design (fixed before the run)

* **Cohort.** RB/WR/TE rows whose last appearance before the cutoff week W is an injury exit
  (`injury_exit_last = 1`), and whose last team played at least one game strictly between that appearance and W.
  These players are returning from an absence, not playing through the injury. 2023-2025: 238 rows at horizon 0
  (RB 50, WR 127, TE 61) and 2,341 at horizons 1-3.
* **Variants.** Each one changes cohort rows only, in training and in prediction alike.
  * `base`: ifp-2.2.0 exactly as shipped. It reproduces `ifp22_backtest.py`'s `pred_full` to 0.0 on all 20,328
    shared rows.
  * `T`: `snap_trend` is set to the position's non-cohort training mean (2020-22 rows). The missing-indicator path
    of `snap_trend_role` is not used, because it would lump the cohort in with players who have fewer than 2 games.
  * **`TU` (the candidate)**: `T`, plus the usage averages recomputed without the injury-exit game (`snap_pct`,
    `carries_pg`, `targets_pg`, `xfp_recent`, `rush_share`, `target_share`, `rz_opps_pg`, `gl_carries_pg`,
    `air_yards_pg`), plus `opp_trend` set to its non-cohort mean.
  * `TUP`: `TU`, plus `pts_recent`, `pts_l3` and `pts_minus_xfp` recomputed the same way (reported only).
* **Walk-forward.** The same as `ifp22_backtest.py`: `context_model.fit`, with every week refitted on horizon-0 rows
  strictly earlier. The store is the ifp-2.2.0 guard's store (the 2026-10-19 scratch copy).
* **Horizons 1-3.** Features as of cutoff W, for players who appear in week W + h. They are built the way
  `independent_projection_store.project` builds a rest-of-season row: target-week opponent, handoff for W + h with
  the walk-forward return-rate table, context with W's report dropped, and role and depth at W.
* **Rule.** `TU` ships iff all three hold:
  * On the pooled cohort at horizon 0, MAE(TU) - MAE(base) has a week-clustered 95% CI (2,000 draws) entirely
    below 0.
  * Its pooled horizon 1-3 difference is <= 0.
  * No position's all-row horizon-0 difference has a CI entirely above 0.

## Result (2023-2025)

"Signed error" is projected minus actual: positive means the projection was too high. The differences are
MAE(variant) - MAE(base), with week-clustered 95% CIs.

| Rows | n | MAE base | MAE TU | TU - base [95% CI] | Signed error base -> TU | T - base | TUP - base |
|---|---|---|---|---|---|---|---|
| **Cohort h0, RB/WR/TE** | 238 | 4.178 | 4.377 | **+0.199 [+0.081, +0.320]** | -0.29 -> +0.42 | +0.136 [+0.041, +0.228] | +0.226 [+0.084, +0.369] |
| Cohort h0 RB | 50 | 5.146 | 5.635 | +0.489 [+0.129, +0.857] | +0.79 -> +1.91 | +0.307 [+0.053, +0.573] | +0.534 [+0.091, +0.974] |
| Cohort h0 WR | 127 | 4.445 | 4.569 | +0.124 [-0.041, +0.283] | -0.81 -> -0.15 | +0.103 [-0.031, +0.230] | +0.153 [-0.047, +0.345] |
| Cohort h0 TE | 61 | 2.830 | 2.946 | +0.116 [-0.074, +0.307] | -0.09 -> +0.39 | +0.061 [-0.054, +0.171] | +0.124 [-0.067, +0.317] |
| **Cohort h1-3, RB/WR/TE** | 2,341 | 4.435 | 4.494 | **+0.058 [-0.009, +0.129]** | -1.10 -> -0.34 | +0.033 [-0.019, +0.087] | +0.094 [+0.010, +0.186] |
| Cohort h1-3 RB | 493 | 4.669 | 4.896 | +0.227 [-0.008, +0.469] | +0.16 -> +1.27 | +0.098 [-0.066, +0.257] | +0.339 [+0.065, +0.626] |
| Cohort h1-3 WR | 1,185 | 4.608 | 4.613 | +0.005 [-0.093, +0.102] | -1.35 -> -0.63 | +0.011 [-0.069, +0.089] | +0.026 [-0.095, +0.144] |
| Cohort h1-3 TE | 663 | 3.952 | 3.980 | +0.028 [-0.063, +0.122] | -1.59 -> -1.02 | +0.026 [-0.036, +0.092] | +0.034 [-0.057, +0.126] |
| All rows h0 | 18,079 | 3.714 | 3.715 | +0.001 [-0.001, +0.003] | -0.02 -> -0.02 | +0.001 [-0.001, +0.002] | +0.002 [-0.001, +0.004] |
| All rows h1-3 | 48,708 | 3.847 | 3.848 | +0.001 [-0.002, +0.005] | -0.34 -> -0.32 | +0.001 [-0.002, +0.004] | +0.003 [-0.002, +0.007] |

Per horizon on the cohort (TU - base): h1 +0.091 [-0.016, +0.203], h2 +0.042 [-0.062, +0.138], h3 +0.044 [-0.073,
+0.163].

**Decision: dropped.** The cohort h0 CI lies entirely *above* 0, the h1-3 point estimate is above 0, and no
position is clearly worse on all rows. The lighter `T` variant (the trend swap alone) is also worse at h0: +0.136
[+0.041, +0.228].

2026 weeks 1-6 (exploratory, cohort h0 n = 29, RB 5): -0.110 [-0.706, +0.080]. The sample is too small to point
either way.

### Lead backs specifically (descriptive, after the decision)

Rows are split by pre-exit healthy snap share (`h_snap_pct >= 0.6`). The columns are signed error base -> TU, and
MAE base -> TU.

| Cohort | n h0 | h0 signed error | h0 MAE | n h1-3 | h1-3 signed error | h1-3 MAE |
|---|---|---|---|---|---|---|
| Lead RB | 21 | +1.12 -> +2.43 | 5.49 -> 6.10 | 217 | +1.12 -> +2.41 | 4.94 -> 5.29 |
| Other RB | 29 | +0.55 -> +1.53 | 4.90 -> 5.30 | 276 | -0.60 -> +0.37 | 4.46 -> 4.59 |
| Lead WR | 85 | -1.12 -> -0.36 | 5.13 -> 5.17 | 799 | -1.69 -> -0.92 | 5.43 -> 5.35 |
| Lead TE | 31 | -0.07 -> +0.60 | 3.89 -> 4.02 | 335 | -1.52 -> -0.70 | 4.65 -> 4.69 |

The owner's concern is lead backs returning from injury, and those are the rows where ifp-2.2.0 is already too
high on average. Barkley's case (hurt on the first carry, then a full return) is real but atypical: the injury-exit
flag cannot tell it apart from a back who comes back limited or re-aggravates the injury. Returning lead WRs are
under-projected, and TU halves that bias, but MAE barely moves (h1-3 -0.08). That split was looked at after the
decision, so it is not evidence for shipping anything.

### Saquon Barkley under each variant (cutoff 2026 week 7, scratch copy)

He is in the cohort (games missed: 2). `snap_trend` is -0.53, and its contribution is **-2.62** points, the
largest single term. Conditional points by target week:

| Variant | W7 | W8 | W9 | W11 | W12 | W13 |
|---|---|---|---|---|---|---|
| base (ifp-2.2.0) | 9.06 | 8.08 | 8.00 | 7.70 | 7.67 | 7.23 |
| T | 10.98 | 9.99 | 9.92 | 9.62 | 9.58 | 9.13 |
| TU | 11.60 | 10.61 | 10.54 | 10.25 | 10.21 | 9.76 |

## Side finding (not pre-registered; a lead only)

The opposite slice was also checked: an injury exit last game, but **no** missed team game since, i.e. a player
who keeps playing. TU leaves these rows' features alone. Their predictions still change, because the cohort rows are
neutralized in training, and that moves the fitted coefficients (the `snap_trend` penalty gets steeper). On this
slice TU is better: h0 -0.066 [-0.117, -0.016] (n = 300), and h1 -0.072 [-0.117, -0.028]. ifp-2.2.0 over-projects
these rows at h0 (+0.65), and TU brings that down to +0.28.

Read plainly: a single `snap_trend` slope is being stretched over two groups. One is players who keep playing after
an exit (the penalty is too small for them). The other is players who return after missing games (the penalty is
about right). This contradicts neither the old ifp-1.0.0 ablation (a blanket swap, slightly worse on injury-exit
rows) nor this note's decision. It was found after looking at the data, so it may only ship through a fresh
pre-registration scored on prospective weeks.

## Reproduce (scratch copies only, from `backend/`)

```
export FANTASY_DB_PATH=<scratch>/fantasy.duckdb NFL_HISTORY_DB_PATH=<scratch>/nfl_history.duckdb \
       WEEKLY_MODEL_DIR=<scratch>/models/weekly IFP_MODEL_DIR=<scratch>/models
uv run python ../scripts/ifp22_backtest.py build                 # the ifp-2.2.0 store (reused, ~10 min if absent)
uv run python ../scripts/injury_return_research.py horizons      # ~1.5 min
uv run python ../scripts/injury_return_research.py run           # ~25 s
uv run python ../scripts/injury_return_research.py example --gsis 00-0034844 --season 2026 --week 7 --weeks 6
```

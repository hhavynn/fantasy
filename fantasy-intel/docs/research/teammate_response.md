# Teammate response: does the model's what-if hold up? (issue #213 v1, research only)

**Outcome, both halves:** both RB and WR **pass the pre-registered rule**, but a diagnostic added after the first
results (a model retrained without the teammate group) **overturns RB**: RB's teammate inputs add nothing measurable
(−0.003, CI includes 0). WR keeps a very small gain (−0.014). No product what-if is built (merge captain's decision);
`scenario_point` stays in the module with no route.

**Status:** research only. Nothing in the app imports `app/analytics/teammate_response.py`
(`tests/test_teammate_response.py::test_research_module_is_never_imported_by_the_app`). No API, UI, table, forecast or
artifact changes. QB scenarios are deferred: no QB scenario challenger was adopted.

## Question

A what-if projection lab would show "if teammate X is out, the model projects this player at …". The only teammate
effect with evidence behind it is the one the production model already ships: ifp-ctx-p1's teammate group in
ifp-2.2.0, for RB and WR (`docs/research/ifp22_promotion.md`, leave-one-out MAE):

- RB: neutral, +0.001 [−0.007, +0.008];
- WR: a small gain, −0.012 [−0.019, −0.005].

That was measured over every player-week. A what-if matters only on the weeks where a teammate's availability actually
changes, so this asks: **on those weeks, is the model's teammate response closer to what happened than two baselines a
manager could use instead?**

## Pre-registration (fixed before the first run)

`DECISION_RULE` in the module, written before the first run (see the timeline for exactly what that rests on). Reports
now carry its SHA-256 (`decision_rule_sha256`, `d4024b44…`) and the checked-out commit (`git_sha`).

- **The model's teammate response.** The model's point with the row's actual teammate inputs, minus its point with
  them neutral. Neutral is 0: no teammate out or returning. Same fitted model, every other input unchanged.
- **Teammate inputs:**
  - RB: `tm_car_gain_pg`, `tm_tgt_gain_pg`, `tm_car_vacated`, `tm_car_returning`;
  - WR: `tm_tgt_gain_pg`, `tm_tgt_vacated`, `tm_tgt_returning`.
- **Event player-weeks.** A known teammate input is non-zero. Unknown inputs stay unknown and are never set to 0.
- **Evaluation window.** 2023 week 1 to 2025 week 18. Walk-forward exactly as production trains: `context_model.fit`
  with the shipped components, refitted every week on strictly earlier weeks, through `ifp22_backtest`'s store.
- **Label.** The player's actual points in the game (the store's `label`, conditional on playing), under the
  production league's scoring (fingerprint `e165b59686110c7d`).
- **Baselines:**
  - **zero:** the same model with the teammate inputs neutral, so no teammate adjustment at all.
  - **split (observational):** zero plus the player's own with/without split. That is his mean points in his
    *earlier* games of the same season that had a teammate change, minus his mean in those that did not, with at least
    2 of each; otherwise no adjustment. Teammates' absences are not random, so this is a correlation, never a causal
    effect. It is a baseline only because a manager could compute it by hand.
- **Decision rule, per position (RB, WR).** The model passes only if MAE(model) − MAE(zero) **and** MAE(model) −
  MAE(split) both have a 95% paired bootstrap interval entirely below 0, under **both** clusterings (by week and by
  player-season; 2,000 resamples, seed 213). Every position is reported whatever the outcome. A product what-if is
  considered only for a position that passes.
- **Also reported, not deciding:**
  - the size of the response (mean, mean absolute, 90th percentile absolute);
  - a calibration slope through the origin: how much of the response showed up in the result;
  - sign agreement;
  - N, players and weeks;
  - how many event rows had a split available;
  - a check that non-event rows are forecast identically by both.

## The scenario core

`scenario_point(model, inputs, changes)` recomputes a stored forecast from its verified full inputs
(`production_forecasts.verified_inputs`, #225 a):

- an unchanged scenario reproduces the stored point exactly (tested on stored runs);
- a change to an input the forecast did not know (imputed) is refused with a reason, as is a change to a column that
  is not a model input;
- a floored point is flagged.

A product would use exactly this. It is built here, with no API, so that the no-op guarantee exists before anyone asks
for a page.

## Data

- nflverse through `nflreadpy` 0.1.5, synced 2026-10-23:
  - finished seasons 2020–2025 in a research history file (`sync_nfl_history.py`);
  - the 2026 season so far in a research "live" file, which holds only nflverse data plus the production league's
    scoring settings and no league data. 2026 is in the training store but never evaluated.
- PBP exits 2020–2025 (`weekly_learning.py sync-exits`) and depth charts 2025–2026 (`sync-depth`; depth-tier coverage
  99.7%).
- Store: 43,101 player-weeks over 113 weeks (`ifp22_backtest.build`). Shipped components for RB and WR:
  `qb_context`, `teammates`, `depth_tier`, `decline_role` (ifp-2.2.0).

## Timeline (pre-registration evidence)

| Time (UTC, 2026-10-23) | What |
|---|---|
| 23:02:56 | `teammate_response.py`, including `DECISION_RULE`, last modified before the run |
| about 23:06 | the run starts, from **uncommitted** files |
| 23:08:08 | pre-registration commit 883224c, while the run was still going |
| 23:08:46 | first results written; read about 23:10 |
| 23:11:29 | first report copied aside (`tr_report_run1.json`); the 23:08:46 file itself was later overwritten |
| after | the retrained diagnostic added, run again; the deciding numbers are identical |
| after review | level-shift, demeaned sign and slope-interval diagnostics added, re-evaluated from the stored predictions (`--from-preds`); the deciding numbers are identical |

What this rests on: the file timestamps above and the author's log, not a commit made before the run (the commit landed
38 seconds before the first results, after the run had started). What independently holds: the run-1 report embeds
the rule text byte-identical to 883224c's `DECISION_RULE`, and has no diagnostics.

## Results (evaluation 2023 week 1 – 2025 week 18; MAE in points per game)

| Position | Event rows / all rows | Players | Weeks | MAE model | MAE zero | MAE split | Model − zero (95% CI by week / by player-season) | Model − split | **Pre-registered decision** |
|---|---|---|---|---|---|---|---|---|---|
| RB | 4,187 / 5,103 (82%) | 430 | 51 | 4.056 | 4.082 | 4.821 | -0.026 (-0.037, -0.016) / (-0.036, -0.017) | -0.765 (-0.879, -0.654) / (-0.915, -0.626) | **pass** |
| WR | 6,542 / 8,120 (81%) | 665 | 51 | 4.050 | 4.086 | 4.791 | -0.036 (-0.045, -0.027) / (-0.044, -0.028) | -0.742 (-0.827, -0.652) / (-0.845, -0.645) | **pass** |

Both positions **pass the pre-registered rule**. The split baseline is weak: raw, unshrunk splits on few games add noise.
Beating it says little, and the zero comparison carries the decision.

### Diagnostic, added after the first results (not deciding): against a model retrained without the teammate group

Setting the teammate inputs to 0 inside a model trained with them can move the inputs off its training distribution.
That would make the zero baseline worse than a real "no teammate adjustment" forecast. The ifp-2.2.0 promotion compared
against the model **retrained without the group**, so the same comparison is repeated here on the event rows:

| Position | MAE retrained without the teammate group | Model − retrained (95% CI by week / by player-season) |
|---|---|---|
| RB | 4.059 | -0.003 (-0.013, +0.006) / (-0.012, +0.004) |
| WR | 4.064 | -0.014 (-0.023, -0.005) / (-0.021, -0.007) |

### Diagnostics added after review (not deciding): how much of the zero gap is a constant

The response's mean on the event rows is negative (−0.063 RB, −0.068 WR). Adding that constant to the zero forecast
(in-sample, so this favours the constant) recovers about half of the zero gap:

| Position | MAE zero | MAE zero + constant | Model − (zero + constant) (95% CI by week / by player-season) | Sign agreement → demeaned |
|---|---|---|---|---|
| RB | 4.082 | 4.069 | -0.014 (-0.024, -0.002) / (-0.023, -0.005) | 58.4% → 53.9% |
| WR | 4.086 | 4.070 | -0.021 (-0.029, -0.012) / (-0.028, -0.013) | 57.3% → 54.4% |

So zeroing the inputs removes the group's average contribution as well as its row-to-row variation. Zero occurs on
about 19% of rows, so "0 is off the training distribution" is not the mechanism. The retrained comparison is the
fairer "no teammate adjustment" forecast.

### Size of the response

| Position | Mean response | Mean absolute | 90th percentile absolute | Calibration slope | Sign agreement | Split available |
|---|---|---|---|---|---|---|
| RB | -0.063 | 0.209 | 0.510 | 1.10 (0.57, 1.67) | 58.4% | 2,503 of 4,187 |
| WR | -0.068 | 0.223 | 0.518 | 1.39 (0.97, 1.81) | 57.3% | 3,779 of 6,542 |

The slope intervals are 95% bootstrap by week (post-hoc). Both include 1, so neither says the response is too small.


## Reading

- **RB: no evidence of a useful teammate response.** RB passes the pre-registered rule against zero (−0.026), but the
  model retrained without the group is as accurate (−0.003, CI −0.013 to +0.006). This matches the promotion's neutral
  RB result (+0.001). The pass comes from the zero counterfactual removing the group's average contribution, which a
  model fitted without the group absorbs elsewhere.
- **WR: a very small gain, shown only by post-hoc diagnostics.**
  - Against the retrained model, −0.014 points per game (CI −0.023 to −0.005 by week, −0.021 to −0.007 by
    player-season): about 0.35% of a 4.06 MAE, close to the promotion's −0.012.
  - This measures the teammate group's predictive value on event weeks. It does not show that a what-if ("if X is
    out") is accurate: no named teammate's status was flipped and checked.
  - Once the level shift is removed, the response's direction is right 54% of the time.
- **The responses are small.** The mean absolute response is about 0.2 points (90th percentile about 0.5), on about
  81% of player-weeks: some teammate input is non-zero most weeks. A what-if page would mostly show changes of a few
  tenths of a point.
- **Decision (merge captain): no product what-if.** `scenario_point` (no-op exact; refuses unknown inputs, null
  indicators, the QB mixture column and non-finite values) stays in the module with no route.

## Limitations

- **The event definition is broad.** It is any non-zero teammate input, not "a named teammate's status flipped this
  week". A per-teammate what-if (one named player in or out) is a narrower change, not evaluated here.
- **The diagnostics were added after the first results** (the retrained comparison, then the level-shift, demeaned-sign
  and slope-interval checks after review). They make the passes weaker, not stronger. The deciding numbers did not
  change.
- **The evaluated counterfactual is wider than the product's.** `neutral` zeroes the known teammate inputs and keeps
  unknown ones unknown. `teammate_neutral` (the product path) refuses the whole scenario when any teammate input was
  imputed. Rows with some teammate input unknown are evaluated here but would be refused in a product.
- **51 of 54 weeks have event rows.** The three without are week 1 of 2023, 2024 and 2025: the teammate inputs come
  from the player's earlier games of the season, so week 1 has none.
- **The split baseline is weak**: raw, unshrunk, earlier games of the season only. A shrunk split would be a fairer
  observational comparator. It is not run here, because it was not pre-registered.
- **The label is points per game played.** Participation (P(plays)) is not part of the teammate response.
- **Scoring and seasons.** One scoring (the production league's), 2023–2025. QB and TE are not evaluated.

# Teammate response: does the model's what-if hold up? (issue #213 v1, research only)

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

Committed with the code before any result was computed: `DECISION_RULE` in the module.

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

## Results

To be filled in from `scripts/teammate_response_research.py` after the pre-registration commit.

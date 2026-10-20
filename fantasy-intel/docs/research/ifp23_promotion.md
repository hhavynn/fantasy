# ifp-2.3.0: the injury-return pair promoted into Ours (owner override) and the backtest guard

**Decision (owner, 2026-10-20).** Promote the "Injury-return pair" into Ours now, by owner override, the same way
ifp-2.2.0 was promoted ([ifp22_promotion.md](ifp22_promotion.md)). Production becomes `ifp-2.3.0`, which is
`ifp-2.2.0` kept whole plus the two frozen candidates of the injury-return shadow
([injury_return_split.md](injury_return_split.md)) on RB, WR and TE. It is recorded in the registry as an
`owner_approval` with `override: true`.

The shadow's pre-registered decision has not happened: its one look is after 2026 week 18, on write-once pregame
ledgers from week 7. Nothing here pretends otherwise. The shadow keeps running unchanged against its frozen ifp-2.2.0
base, and its ledgers are the post-hoc evidence.

## Components (frozen definitions, no refit choices)

A **returner** is an RB/WR/TE whose last appearance before the cutoff week was an injury exit
(`injury_exit_last = 1`) and whose last team has played at least one game since (`games_missed >= 1`, counted up to
the cutoff week). These are the shadow's definitions (`injury_return_research.with_cohort`), re-implemented in
`app/analytics/injury_return_model.py` and tested equal to it.

| Component | Shadow | Adds | Positions |
|---|---|---|---|
| `return_slope` | `ifp-irs-p1` | `snap_trend_ret = snap_trend if returner else 0`: returners get their own `snap_trend` slope | RB, WR, TE |
| `return_bias` | `ifp-irb-p1` | `ret_ind = 1 if returner else 0`: each position's own returner adjustment | RB, WR, TE |

Everything else is ifp-2.2.0 exactly: features, training rows, frozen penalties, loss and recency, the QB model
(scenarios, start model, fallback) and P(plays). QB is unchanged.

## The backtest guard (fixed before the run)

This is the same owner-approved safeguard as ifp-2.2.0 (`scripts/ifp22_backtest.py`), applied with
`scripts/ifp23_backtest.py`:

* **Harness.** The ifp-2.2.0 guard's store (`ifp22_backtest.build`) plus the two columns. Walk-forward exactly as
  production trains (`challenger.train_rows` / `fit_linear` with ifp-2.2.0's frozen penalties and recency), every
  week refitted on strictly earlier weeks.
* **Window.** Every fully final week from 2023 week 1 on (ifp22_backtest's `EVALUATION`), on identical player-weeks.
* **Variants per position (RB, WR, TE).** `b22` (ifp-2.2.0), `full` (both components), and each leave-one-out
  (`drop_return_slope`, `drop_return_bias`).
* **Rule.** A component whose leave-one-out difference, MAE(full) - MAE(drop_c), has a week-clustered 95% CI
  (2,000 draws) entirely above 0 makes the position clearly worse and is left out for that position. Every neutral or
  better component ships. A second pass re-checks the shipped set the same way.
* **Empty result.** If no component survives at any position, nothing ships and no version is made.
* **Reported, not deciding.** `full` (as shipped) vs `b22` per position, on all rows and on the returner rows.

## Results

Not run yet.

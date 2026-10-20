# Injury-exit players: split `snap_trend` slope and returner bias (pre-registered, prospective)

Two leads came out of the injury-return study (hhavynn/fantasy#135,
[injury_return_snap_trend.md](injury_return_snap_trend.md), "Side finding" and "Lead backs"). Both were found
*after* looking at 2023-2025, so neither can be judged on those seasons. This note fixes the candidates, the
evaluation window and the decision rule **before any number for them exists**. It was committed on its own,
ahead of any code or run (`git log --follow` on this file shows the order).

## 1. Pre-registration (fixed 2026-10-19, before any run)

### 1.1 Why the evaluation window is prospective

The rule is: the validation window decides, and it must not be data the leads came from. No untouched season
exists:

| Window | Status |
|---|---|
| 2020-2022 | ifp-2's development window ("development 2021-22 (all choices)", independent_projection_engine.md); the injury-exit descriptive study also covered 2020-2024 |
| 2023-2025 | the window #135 inspected; both leads come from it |
| 2026 weeks 1-6 | inspected by #135 (exploratory cohort numbers) |
| 2026 week 7 | the main slate kicked off on 2026-10-18, before this note: a pregame forecast can no longer be frozen |

So the deciding window is **prospective: write-once pregame ledgers from 2026 week 8 on**, through the existing
research-shadow machinery (`scripts/injury_return_shadow.py`, modeled on `ctx_shadow.py` / `signal_shadows.py`;
run by `refresh_all.py`'s evidence stage and by `learning_cycle.py`'s `RESEARCH_SHADOWS`; watched by
`shadow_promotion_check.py`).

### 1.2 Definitions

All on ifp-2.2.0's RB, WR and TE models (`context_model.SHIPPED`); QB is not touched.

* `injury_exit_last`: the existing ifp feature (a last-game snap drop of at least 15 points plus an injury
  listing within two weeks, reports up to the target week's pregame report). It implies `snap_trend <= -0.15`,
  so `snap_trend` is never null on these rows.
* `games_missed`: games the player's last team played strictly between his last appearance and the target week
  (`injury_return_research.with_cohort`, #135).
* **Play-through row**: `injury_exit_last = 1` and `games_missed = 0` (hurt last game, playing again now).
* **Returner row** (`ret`): `injury_exit_last = 1` and `games_missed >= 1`.

### 1.3 Candidates (each judged on its own)

| Candidate | Change to ifp-2.2.0 (RB, WR, TE) | Cohort it is judged on |
|---|---|---|
| `ifp-irs-p1` (split slope) | adds `snap_trend_ret = snap_trend if ret else 0`. The existing `snap_trend` slope is then fitted by the non-returner rows (normal and play-through), and returners get their own slope (`snap_trend` + `snap_trend_ret`). Returners' features are otherwise untouched. | every `injury_exit_last = 1` row (play-through **and** returner rows: the change moves both) |
| `ifp-irb-p1` (returner bias) | adds `ret_ind = 1 if ret else 0`. Each position has its own ridge, so this is the position x returner indicator. | returner rows |

Shared, nothing searched: ifp-2.2.0's features, training rows (`challenger.train_rows`: final weeks strictly
before the target week, 2020 on), frozen penalty, loss and recency half-life per position. ifp-2.2.0 (`base`)
and both candidates are fitted in the same run on the same rows. The combination of both (`both`) is also frozen,
**reported only**; it matters only in rule 1.6 (both pass).

### 1.4 What is frozen each week

At the first pregame run at which at least 50% of the teams that have not kicked off do so within 6 h
(signal_shadows' freeze rule), one write-once ledger per week holds, for every RB/WR/TE in the live population
of the open teams: `base`, `irs`, `irb` and `both` conditional points, plus `injury_exit_last`, `games_missed`,
`snap_trend`. Teams that have kicked off are excluded. Features are built as production builds a horizon-0 row:
`features_for_week`, then `injury_model.augment` and `context_model.augment` with the same-run ifp-2.2.0
artifact. Horizons 1-3 are not frozen and play no part in the decision.

### 1.5 Decision rule (per candidate)

Scored rows: frozen pregame rows of fully final weeks where the player appeared (a label exists) and `base` and
the candidate both forecast. Differences are MAE(candidate) - MAE(base), with a week-clustered bootstrap 95% CI
(2,000 draws, seed 20261019, `forecast_backtest.week_bootstrap_mae_diff`).

**One look only.** The decision is taken once, at the first scoring at which every 2026 regular-season week has
been scored (week 18 fully final), on all frozen weeks from week 8. If at that look fewer than 8 weeks are scored
or the cohort has fewer than 40 rows, the look moves to the first later scoring (2027 weeks) at which both
minimums hold. Before the look the status is `accumulating`; interim numbers are shown but never decide. The
decision is then written once (`decision_<candidate>.json`) and never re-evaluated.

The candidate **passes** iff all hold:

1. Cohort (RB, WR, TE pooled): the CI lies entirely below 0.
2. Overall (all scored RB, WR, TE rows pooled): MAE difference <= 0.
3. No position's all-row CI lies entirely above 0.

Otherwise it **fails**: it is recorded in this note and dropped (no-gain candidates are not shipped or kept as
shadows).

### 1.6 What a pass does

A pass is reported by `shadow_promotion_check.py` as `PROMOTION READY` and queued with its `plan`. The fold-in
follows ifp-2.1/2.2 (docs/shadow_promotion.md): a reviewed PR builds **ifp-2.3.0** = ifp-2.2.0 plus the passing
candidate's feature on RB, WR and TE, registered with `approved_production` citing this shadow's evidence, the
candidate added to `PROMOTED`. If both pass, ifp-2.3.0 takes `both` only if `both` also meets rules 1-3 on each
cohort and its overall MAE is no higher than the better single's; otherwise the single with the lower overall
MAE. The scheduled job never edits production.

## 2. How it runs (added after the pre-registration commit; section 1 is unchanged)

`scripts/injury_return_shadow.py` (research only: it writes under `data/models/research/injury_return_shadow/`,
opens the databases read-only and never touches production, the registry, the API or the frontend):

| Job (existing) | Runs | Does |
|---|---|---|
| `refresh_all.py` evidence stage (every refresh and pregame-guard run) | `injury_return_shadow.py pregame --week <upcoming>` | the gated, write-once freeze. **Pending:** this step is added in a follow-up after hhavynn/fantasy#136, which also edits `refresh_all.py`, merges. Until then only the cycle's retry can freeze a week, and it normally defers, so weeks before the follow-up are usually recorded as missed |
| `learning_cycle.py`, `RESEARCH_SHADOWS` stage `injury_return_shadow` (Tue-Thu) | `injury_return_shadow.py cycle --week <target>` | scores fully final frozen weeks (and takes the look once due), records a missed week, retries the freeze (normally deferred) |
| `learning_cycle.py`, last stage `promotion_check` | `shadow_promotion_check.py check` | reads `scores.json`; a pass is `PROMOTION READY: ifp-irs-p1 ...` plus a promotion-queue entry |

* **Frozen spec.** `frozen/spec.json` is written once by the first freeze: the added columns, the cohort and
  returner definitions, the rule constants, the freeze rule and the league-scoring fingerprint. A later run with
  any other spec is refused. The spec also freezes the ifp-2.2.0 base: `context_model.MODEL_VERSION`, its
  config hash and `challenger.json`'s hash. A later freeze on a moved base is refused, every ledger records the
  base it was fitted with, and `score` refuses to pool a ledger whose base differs from the spec's. So the look,
  even one deferred into 2027, compares against the base that was registered. If production moves, the shadow
  stops and says why; it does not silently switch bases.
* **Exact gate.** The gate and the both-choice compare unrounded MAE differences and CI bounds.
* **Both pass.** `shadow_promotion_check.py` honors `both.choice`: only the selected implementation (one single,
  or both together) is marked ready and queued.
* **Same-run base.** The `base` column is ifp-2.2.0 refitted in the run by `context_model`'s own rule; on the
  2026 week 8 smoke test it equals `context_model.fit` exactly (maximum prediction difference 0.0 for RB, WR and
  TE).
* **Ledger rows.** Every RB/WR/TE of the open teams' live population (ACT/INA weekly roster), with
  `base_points`, `irs_points`, `irb_points`, `both_points`, `ret` and the inputs `injury_exit_last`,
  `games_missed`, `snap_trend`. Cohort membership is read from the ledger, i.e. as it was known pregame.
* **Availability news** (#134) is a production overlay on the live run (`build_live`'s `news=`), not part of the
  model. The shadow passes none, the default, which changes nothing. Base and candidates share the same rows either
  way.
* **After the look.** Once both candidates have a decision, `pregame` stops freezing (`status: decided`). A failed
  candidate is then recorded in section 3 and dropped.

Smoke test (scratch copies, 2026-10-19, week 8 frozen into a scratch directory only): 82 s end to end, 377 rows
for the 28 teams not on bye, 8 returners. Saquon Barkley (games missed: 3) is base 7.21, `irs` 7.52, `irb` 7.44.
Play-through rows appear once the week-8 injury report lands, because `injury_exit_last` needs the listing.

## 3. Results

None yet. The first ledger is 2026 week 8; the look is after week 18.

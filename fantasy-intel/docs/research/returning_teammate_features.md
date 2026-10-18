# Returning-teammate challenger (`ifp-ret-0.1.0`, research only)

**Question.** The ifp-ctx study (`qb_teammate_context.md`, section 7) found the *returning* half of the teammate group to be its most consistent
signal. Does a small challenger with **only** returning-teammate effects beat ifp-2? It adds no
vacated volume, no redistribution of absent players' opportunity and no QB context.

**Short answer: no, and the reason is informative.**

* **Not adopted.** No position meets the development rule. Pooled R minus ifp-2 MAE is +0.000
  [-0.005, +0.006] in development, -0.005 [-0.009, -0.000] in confirmation and +0.001 [-0.005, +0.006]
  in the replay. Verdict: **`retain_baseline`**.
* **The returning signal is real and replicates in all three windows.**
  * RB rows when the team's RB1 is back: -0.32 / -0.37 / -0.35 MAE. ifp-2's over-projection there
    (bias about -1.0) almost disappears.
  * WR rows when the WR1 is back: -0.06 / -0.06 / -0.16.
  * Rows whose role-holder competitor comes back this week: -0.054 / -0.047 / -0.075 pooled.
* **It is paid back on the rows without a returner.** That is about 60% of rows. For RB and WR their MAE
  worsens by +0.013 to +0.036 in every window, with every CI above 0. TE and QB are about 0. The features are one-sided
  (always >= 0), so a measured 0 sits below the training average. The fit therefore lifts every
  non-returner a little, which cancels the cohort gain.
  * B's signed `tm_*_gain_pg` (vacated +, returning -) keeps 0 near the average, so B pays nothing on
    those rows: about -0.001 in every window.
* **Against B on the same rows.**
  * R matches B on the "back this week" cohort: -0.054 / -0.047 / -0.075 vs -0.055 / -0.047 / -0.082.
  * R is clearly better for RBs whose RB1 returns: -0.32 / -0.37 / -0.35 vs -0.19 / -0.17 / -0.23.
  * R is worse for WRs whose WR1 returns: -0.06 / -0.06 / -0.16 vs -0.13 / -0.12 / -0.19.
  * Pooled, B stays ahead: -0.014 / -0.015 / -0.019 vs about 0. B's numbers here reproduce `qb_teammate_context.md`
    sections 6-7 exactly.

Code: `backend/app/analytics/returning_context.py` (features, candidate, walk-forward, cohorts,
pre-registered rules), `scripts/returning_research.py` (`build`, `develop`, `evaluate`), tests in
`backend/tests/test_returning_context.py`. Outputs go to `data/models/research/returning/` (git-ignored),
with the ifp-ctx store cached in its own `work/` subdirectory. ifp-ctx, **`ifp-ctx-p1` and its ledger are
not touched** (tested), and nothing in the app imports the module (tested). ifp-2, the registry and
production are unchanged.

## 1. Definitions (fixed before the first backtest; `returning_context.py`)

* **Role holders.** Per team, among players not off the team, there are three role holders.
  * **WR1** and **TE1**: the highest target share when playing at the position.
  * **RB1**: the highest carry share when playing.
  * "When playing" covers the player's last 5 games for the team: the current season first, then the
    previous season. A WR1 hurt in week 2 keeps the role.
  * At least 2 games are needed. A player with no current-season game for the team needs current-season
    roster evidence; this repeats the `ctx-0.1.1` fix.
* **Returning.** A role holder (not the player himself) who missed some of the player's window games
  (as in `qb_teammate_context.md` section 3) and is expected back in W. That means available or Questionable on the W report
  and not off the team. If he missed the team's last game, he also needs pregame evidence: listed on
  the W report, or an active roster status before W.
  * Unlike ifp-ctx there is **no 6-week staleness cut-off**, because long absences are the point.
  * **Reserve route:** non-active roster status before W but listed on the W report without
    Out/Doubtful (a practice window opened). This is flagged `ret_from_reserve`.
* **Features.**
  * `ret_tgt_reclaim` = sum of the returners' target share when playing x the fraction of the
    player's window games they missed. `ret_car_reclaim` is the same for carries.
  * `ret_tgt_loss_pg` = (s - s / (1 + reclaim)) x team targets per game, the targets per game the
    player cedes. `ret_car_loss_pg` is the same for carries.
  * Groups: QB `ret_tgt_reclaim`; RB `ret_car_loss_pg, ret_tgt_loss_pg, ret_car_reclaim`; WR and TE
    `ret_tgt_loss_pg, ret_tgt_reclaim`.
  * Without a week-W report every feature is null. No returner is a measured 0.
* **Protocol.** Identical to `qb_teammate_context.md` section 4 and its thresholds. One candidate, R, is ifp-2's spec, frozen
  penalty and recency weights plus the group. It is adopted per position on development 2021-22 only
  (MAE diff <= -0.010, CI upper < 0, |bias| not worse by > 0.15). Confirmation checks c1-c6 apply as there.
  * **c7 (mechanism)**: MAE diff < 0 on each adopted position's rows whose role-holder competitor
    comes back this week (`ret_back_now`: he missed the team's last game).
  * ifp-ctx's B is fitted on the same rows as a **descriptive comparator** only.
  * The ifp-2 column is asserted equal between both walk-forwards, and tested equal to
    `challenger.challenger_predictions`.
  * Rules are in `returning_context.DECISION_RULES`, committed before the first development run.
* **One disclosed change before any points backtest.** The first `build` showed that `ret_any` (a
  returner missed *any* window game, even weeks ago) covers 31% of rows. So the mechanism cohort was
  changed to `ret_back_now` (10% of rows) and committed. No points labels were looked at.

Coverage: features are null for 10-11% of 2020-25 rows (week 1, a player new to his team) and 26% of
2026 weeks 1-6. Cohort sizes over 2020-26: back this week 3,862 rows; WR1 returning 4,017; TE1 4,685;
RB1 4,555; reserve route 370.

## 2. Development (2021-22) and the frozen selection

Identical rows (n = 13,372, 36 weeks). Week-clustered 95% CIs (1,000 resamples).

| Position | ifp-2 MAE | R minus ifp-2 | Bias ifp-2 -> R | B minus ifp-2 (descriptive) | Adopted |
|---|---|---|---|---|---|
| QB | 6.008 | +0.008 [-0.007, +0.023] | -0.525 -> -0.536 | -0.000 | ifp2 |
| RB | 4.296 | -0.005 [-0.020, +0.011] | -0.117 -> -0.140 | -0.031 [-0.050, -0.011] | ifp2 |
| WR | 4.438 | +0.002 [-0.002, +0.006] | -0.281 -> -0.283 | -0.016 [-0.026, -0.005] | ifp2 |
| TE | 2.990 | +0.001 [-0.003, +0.004] | -0.060 -> -0.060 | +0.001 | ifp2 |
| All | 4.202 | +0.000 [-0.005, +0.006] | -0.208 -> -0.216 | -0.014 [-0.021, -0.007] | |

Frozen write-once: `frozen_selection_ifp-ret-0.1.0.json` (every position `ifp2`). With nothing adopted,
the outcome is `retain_baseline` by rule. The confirmation numbers below are descriptive and decide
nothing.

## 3. All windows, by position

R minus ifp-2 MAE, with B minus ifp-2 in the last column. conf = 2023-24
(n = 13,345); expl = 2025-26 (n = 8,645, retrospective injury reports).

| Position | dev | conf | expl | B: dev / conf / expl |
|---|---|---|---|---|
| QB | +0.008 | +0.002 | +0.003 | -0.000 / +0.007 / -0.010 |
| RB | -0.005 | -0.011 [-0.025, +0.003] | +0.006 | -0.031 / -0.014 / -0.034 |
| WR | +0.002 | -0.004 [-0.008, +0.001] | -0.004 | -0.016 / -0.024 / -0.026 |
| TE | +0.001 | -0.002 | +0.002 | +0.001 / -0.010 / +0.006 |
| All | +0.000 | -0.005 [-0.009, -0.000] | +0.001 | -0.014 / -0.015 / -0.019 |
| All RMSE (ifp-2 -> R) | 5.872 -> 5.871 | 5.716 -> 5.712 | 5.790 -> 5.785 | |

Starters pool (All): -0.004 / -0.011 [-0.019, -0.004] / -0.001. By season in the replay: 2025 +0.000,
2026 weeks 1-6 +0.003.

## 4. By returning-player role

R minus ifp-2 MAE on the rows of the given position, by which role holder is returning (a TE row with
"TE1 returning" is a TE2 whose TE1 is back). n is dev / conf / expl. CIs are shown where they exclude 0.

| Rows | Cohort | n | dev | conf | expl | Bias ifp-2 -> R (conf) |
|---|---|---|---|---|---|---|
| RB | RB1 returning | 365 / 286 / 100 | -0.321 [-0.448, -0.196] | -0.372 [-0.500, -0.254] | -0.352 [-0.564, -0.133] | -0.85 -> +0.07 |
| RB | back this week | 325 / 336 / 139 | -0.121 [-0.228, -0.009] | -0.135 [-0.225, -0.059] | -0.086 | -0.57 -> -0.24 |
| RB | WR1 returning | 346 / 330 / 157 | -0.008 | -0.013 | +0.022 | |
| RB | TE1 returning | 493 / 441 / 190 | +0.004 | -0.011 | +0.033 [+0.008, +0.056] | |
| RB | no returner | 2,177 / 2,148 / 1,536 | +0.036 [+0.029, +0.043] | +0.028 [+0.023, +0.034] | +0.029 [+0.022, +0.035] | +0.04 -> -0.08 |
| WR | WR1 returning | 497 / 443 / 225 | -0.062 [-0.083, -0.039] | -0.057 [-0.077, -0.038] | -0.157 [-0.228, -0.103] | -0.65 -> -0.45 |
| WR | TE1 returning | 736 / 699 / 302 | -0.031 [-0.044, -0.017] | -0.031 [-0.050, -0.010] | -0.067 [-0.110, -0.035] | |
| WR | RB1 returning | 713 / 649 / 202 | -0.021 [-0.034, -0.007] | -0.023 [-0.041, -0.006] | -0.026 | |
| WR | back this week | 515 / 543 / 237 | -0.045 [-0.069, -0.022] | -0.035 [-0.056, -0.009] | -0.128 [-0.201, -0.052] | -0.14 -> +0.03 |
| WR | no returner | 3,158 / 3,364 / 2,411 | +0.018 [+0.015, +0.022] | +0.013 [+0.011, +0.016] | +0.022 [+0.018, +0.027] | +0.22 -> +0.15 |
| TE | WR1 returning | 391 / 326 / 169 | +0.014 | -0.008 | -0.024 | |
| TE | TE1 returning | 331 / 312 / 144 | +0.001 | -0.005 | -0.024 [-0.043, -0.009] | |
| TE | RB1 returning | 434 / 404 / 116 | +0.005 | +0.007 | -0.021 [-0.039, -0.005] | |
| TE | back this week | 303 / 312 / 139 | +0.009 | +0.005 | -0.033 [-0.059, -0.006] | |
| TE | no returner | 1,951 / 2,031 / 1,508 | -0.001 | +0.001 | +0.011 [+0.009, +0.014] | |
| QB | WR1 returning | 134 / 122 / 52 | -0.007 | -0.015 | -0.080 | |
| QB | back this week | 118 / 130 / 52 | -0.069 | +0.012 | +0.079 | |
| All | back this week (mechanism) | 1,261 / 1,321 / 567 | -0.054 [-0.085, -0.024] | -0.047 [-0.073, -0.020] | -0.075 [-0.120, -0.032] | -0.13 -> +0.07 |
| All | long absence (missed >= 60% of the window) | 1,190 / 1,266 / 463 | -0.078 [-0.115, -0.040] | -0.067 [-0.103, -0.026] | -0.137 [-0.196, -0.080] | -0.26 -> +0.02 |
| All | reserve route | 53 / 203 / 45 | n/a (n < 30 per position) | -0.065 [-0.161, +0.023] | n/a | -0.49 -> -0.18 |
| All | ifp-ctx "top target competitor returning" | 1,555 / 1,420 / 747 | -0.040 [-0.061, -0.021] | -0.017 [-0.033, -0.001] | -0.044 [-0.062, -0.022] | |
| All | no returner | 8,008 / 8,341 / 6,010 | +0.019 [+0.016, +0.022] | +0.013 [+0.011, +0.016] | +0.020 [+0.017, +0.022] | +0.20 -> +0.13 |

Reading:

1. **RB1 returning is the strongest and most stable teammate effect found in either study.** A backup's
   recent stats were earned with the lead back out. ifp-2 projects him about 1 point too high. R
   removes almost all of that bias and 0.32-0.37 MAE, in every window. It is the mirror of ifp-ctx's
   failed "RB1 out" cohort: losing volume to a known lead back is predictable, while inheriting it is
   right-skewed.
2. **For receivers, the WR1 coming back helps every window. TE1 and RB1 returning help WRs a little.**
   B does better on the WR1 cohort because its gain feature also uses the vacated side of the same
   budget.
3. **TE and QB rows gain nothing reliable**, which answers `qb_teammate_context.md` section 11 item 4 ("worth testing for TE").
4. **Why the pooled number is flat.** On no-returner rows, R raises projections by about 0.08
   points in every window. RB/WR MAE rises by 0.013-0.036 and pooled MAE by 0.013-0.020. Their bias moves toward
   over-projection in development and the replay, and toward 0 in confirmation. That is about
   60% of rows at +0.02 against about 10-30% at -0.05 to -0.35. This is a property of one-sided
   features in a single linear fit, not of the signal.

## 5. Recommendation and next work

**Retain ifp-2. Leave `ifp-ctx-p1` frozen as it is** (its prospective evidence is unaffected; nothing
here changes it). Do not pre-register R itself.

The cohort results point to two follow-ups. Both need a new pre-registration, and both can only be
confirmed prospectively, because 2021-26 has now been inspected with these features:

1. **A cohort-restricted returning adjustment (`ifp-ret-0.2`).** Fit the returning coefficients on ifp-2's
   walk-forward residuals using only rows with a returning role holder, and apply them only there. Rows
   without a returner then equal ifp-2 exactly, by construction. That removes the cost that cancels R's
   gain. Start with RB (RB1 returning) and WR (WR1 returning), where the effect replicates.
2. **RB1-returning features inside B for RB rows.** B's signed budget avoids the no-returner cost but
   gains only about half as much as R on the RB1-returning cohort.

Either would need its own write-once ledger, like `ifp-ctx-p1`'s, and must not modify that ledger.

## Reproduce

```
cd backend
uv run python ../scripts/returning_research.py build       # ifp-ctx store (own cache) + returning features, cohort sizes
uv run python ../scripts/returning_research.py develop     # 2021-22 only; writes frozen_selection_ifp-ret-0.1.0.json once
uv run python ../scripts/returning_research.py evaluate    # 2023-24 + 2025-26, evaluation.json
```

Runtime on the local machine: about 30 seconds to build, and a few seconds per walk-forward window.

# Opponent pass-direction profile: does it predict where opponents throw? (issue #198 step 2, research only)

**Outcome: fail.** Knowing where opponents have thrown against a defense does not improve the prediction of where the
next opponent throws, compared with that offense's own tendency or with the league mix. **No route or page follows.**
Nothing in the app imports `app/analytics/pass_direction.py`
(`tests/test_pass_direction.py::test_research_module_is_never_imported_by_the_app_and_writes_nothing`), and no
forecast, table or page changes.

## Question

A matchup page could show "opponents throw left-short against this defense more than the league does". Before such a
line is shown, this asks whether the profile carries any information about the next game. It sets aside whether it
explains anything about coverage, which `docs/research/defensive_matchup_sources.md` shows the free data cannot
support.

## Pre-registration

The rule was fixed in code before any real data was run (`DECISION_RULE` in the module). Its SHA-256 is
`bc0a9f5c…`.

* **Cells.** PBP `pass_location` (left, middle, right) × `pass_length` (short, deep), making six cells. They are counted
  on regular-season pass attempts, excluding sacks and two-point tries. An attempt with either field unknown is
  counted as unknown and never assigned a cell.
* **Predictions** for each attempt, each built from same-season games strictly before its week:
  * **league:** the league-wide cell share, with one pseudo-attempt per cell so that no cell is ever 0.
  * **offense:** the offense's own share, shrunk toward league with 100 pseudo-attempts.
  * **combined:** the offense share × (defense allowed share ÷ league share), with the defense share shrunk the same
    way. The defense adjustment is used **only in cells with at least 30 prior defense attempts**. Every other cell
    keeps the offense share unadjusted, never 0, and the six cells are then renormalized.

  Both baselines are fair comparators, per the #212 lesson: neither is zero, and neither is the last game.
* **Window.** Seasons 2022–2025, weeks 5–18.
* **Metric.** Mean per-attempt log loss.
* **Decision.** The profile adds signal only if LL(combined) − LL(offense) **and** LL(combined) − LL(league) both have
  a 95% paired bootstrap interval entirely below 0 under **both** clusterings:
  * by season-week;
  * by season-defense.

  The bootstrap uses 2,000 resamples with seed 198.
* **Presentation.** Any shown profile is labelled "Unadjusted for opponent strength. Throw direction from the
  offense's side; no defender is named or paired."

### Timeline (UTC, 2026-10-24)

| Time | What |
|---|---|
| 01:27:42 | pre-registration commit dc8d04d (module, rule, tests, script); pushed 01:27:44 |
| 01:27:51 | first and only run starts from that commit; the report records `git_sha` dc8d04d, clean |
| 01:28:06 | results written |
| after | post-hoc checks: mine, and the independent reviewer's sweep (below), labelled as such. The deciding numbers are unchanged |
| after review | the script's report key `weeks` renamed `eval_weeks`. It had been overwritten by `evaluate()`'s count of season-weeks, so `report.json` recorded 56 instead of [5, 18]. The module and the rule are unchanged |

## Data

* nflverse play-by-play through `nflreadpy` 0.1.5, seasons 2022–2025, downloaded 2026-10-24.
* 71,354 pass attempts. Only 3 had an unknown cell.
* The evaluation covers 54,512 attempts: 831 games across 56 season-weeks, at 13,238–13,912 attempts a season.
* League cell mix:

  | | left | middle | right |
  |---|---|---|---|
  | **short** | 30.6% | 17.5% | 33.7% |
  | **deep** | 7.2% | 3.5% | 7.4% |

## Results

| Prediction | Mean log loss per attempt |
|---|---|
| league | 1.53624 |
| offense | 1.53591 |
| combined (defense profile) | 1.53603 |
| *(uniform over 6 cells, for scale)* | *1.79176* |

| Gap (log loss) | Difference | 95% CI by season-week | 95% CI by season-defense | Pre-registered |
|---|---|---|---|---|
| combined − offense | +0.00013 | (−0.00064, +0.00092) | (−0.00068, +0.00093) | **fail** |
| combined − league | −0.00021 | (−0.00158, +0.00113) | (−0.00175, +0.00138) | **fail** |

**Decision: fail.** Neither interval lies below 0. Against the offense's own tendency, the point estimate goes the
wrong way.

### Not deciding

* **Abstention.**
  * A defense had at least 30 prior attempts in 3.48 of the 6 cells on average, mostly the three short cells.
  * Every evaluated attempt had at least one adjusted cell.
  * All 6 cells were adjusted on 0.3% of attempts. Middle-deep is almost never adjusted (0.7% of attempts); left-deep
    and right-deep are adjusted about a third of the time.
* **Per-game cell-share MAE** (realized vs predicted shares): league 0.0529, offense 0.0530, combined 0.0531. The
  defense profile is no better on this measure either.
* **Post-hoc, after the result** (none of this decides anything):
  * **Offense vs league at the pre-registered shrinkage (100):** −0.00034, with CIs (−0.00137, +0.00063) and
    (−0.00156, +0.00091), so not measurable at that setting. **The setting under-shrinks:** at shrinkage 400 the gap is
    −0.00226, with CIs (−0.00288, −0.00167) and (−0.00299, −0.00152), below 0 under both clusterings. Offenses' own
    direction tendencies are real but small. (I reproduced both numbers with `pdr._ll_gap(rows, "p_offense",
    "p_league")` on the predictions, setting `SHRINK` to each value.)
  * **The reviewer's robustness sweep** of the defense adjustment, an independent post-hoc script:
    * The pre-registered model is, if anything, under-regularised rather than over-penalising the defense. The
      30-attempt gate helps at every shrinkage tried. Weaker defense shrinkage, or a stronger ratio exponent, makes
      the combined model worse.
    * Damped variants beat the offense prediction in-sample: a ratio exponent of 0.5 gives −0.00092, and defense
      shrinkage of 400 gives −0.00083, both with CIs below 0. Each still fails against the league mix under the
      season-defense clustering, and every gain comes from 2022–23. In 2024–25 every interval includes 0.
    * An exponent fitted on 2022–23 (0.5) gives −0.000085 on 2024–25, with a CI of (−0.0008, +0.0007).
    * Additive, opponent-adjusted, depth-only and location-only variants also fail.

    No reasonable variant passes the full rule.

## Reading

* **At this cell grain, most of the predictable part of where a pass goes is the league mix.**
  * All three predictions sit within 0.0004 nats of each other, against 0.26 nats between any of them and a uniform
    guess.
  * Offense tendencies add a small, real amount, but only with heavier shrinkage (post-hoc).
  * This test did not model situation (down, distance, score) or alignment.
* **Under the pre-registered model the defense profile adds no detectable signal.**
  * The intervals rule out a gain larger than about 0.0007 nats per attempt over the offense prediction.
  * Damped post-hoc variants reach at most about 0.001 nats, and that does not replicate in 2024–25.
  * Defenses do differ in what they allow, partly because of their schedules. Those differences are not a usable
    next-game signal at this grain. A "throws left-short against this defense" line would not be backed by evidence.
* **Recommendation:** no matchup indicator built on the defense's pass-direction profile, on any page. Per
  `docs/research/defensive_matchup_sources.md`, the remaining candidates are:
  * last season's man/zone mix (participation; license check pending);
  * FTN blitz and box rates (CC-BY-SA decision pending).

  Either would need its own pre-registered test.

## Limitations

* **Granularity.** The cells are coarse: three directions × two depths. A finer grain, such as air-yard bins, or a
  receiver-level target model was not tested, because it was not pre-registered.
* **Shrinkage.** The shrinkage strength (100) and the 30-attempt threshold were fixed in advance and not tuned for
  the decision. The post-hoc sweep above shows that heavier shrinkage helps both the offense and the defense
  predictions slightly, and that no variant changes the decision.
* **Sample.** Weeks 5–18 only, within a season. A defense's profile carried over from the prior season was not tested.
* **No opponent adjustment.** This matches the label the profile would carry.
* **Scope.** The test measures prediction of attempt location, not efficiency. EPA per cell was not tested.
* **Licensing.** The PBP per-file license is still unverified (`defensive_matchup_sources.md` §3). It does not matter
  here, since nothing is published.

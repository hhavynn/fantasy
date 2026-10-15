# ifp-2 research challenger and weekly learning: technical report

Date: 2026-10-15 (2026 NFL week 6, before Thursday's kickoff). Data: local nflverse history 2019-2025 and
live 2026 weeks 1-5 (all 79 games final; confirmed against `nfl_schedules` and `sync_runs`, nflverse
synced 2026-10-15 08:15 UTC). Engine documentation: `docs/independent_projection_engine.md`, section
"Weekly learning and the ifp-2 challenger".

**Bottom line.** The challenger is a real but small improvement: pooled MAE -0.4% on a confirmation
window it was frozen before seeing (95% CI excludes zero), consistent on the exploratory replay, with
its clearest gains after genuine role drops (-3.5%) and team changes (-1.9%). It does **not** forecast
better after injury exits; it only removes C's systematic under-projection there. Huber loss, the
touchdown/yardage split, extra context and healthy-usage replacement were rejected. Nothing is
promoted: the challenger runs in shadow and must earn promotion on frozen pregame forecasts from week
6 onward.

## 1. Audit of the existing engine (Phase 0)

| Question | Finding |
|---|---|
| How do completed 2026 weeks enter training? | Only when `fit` is run. It rebuilds the feature store for every week in the panel and refits the frozen selection on all labeled rows, then overwrites the single artifact file. |
| Does `fit` update regularly? | No schedule and no gate: an unfinished game is simply absent from the panel, and nothing checks that a week is complete or that xFP/snaps are published. The 2026-10-15 09:01 artifact used weeks 1-5 (verified: my weekly refit reproduces its coefficients exactly). |
| Are labels, features, live inputs and predictions separated? | Yes for features (`features_for_week` re-applies the cutoff) and labels (`build_store`). Live predictions are stored per week but `project` replaces the week's rows, so a post-kickoff rerun erases the pregame forecast; only `artifact_created_at` records which model produced it. |
| Do extreme outcomes move coefficients or recent form? | Squared loss: a 40-point game has full influence on coefficients, and enters `pts_recent`/`pts_l3`/`pts_std`/`pts_minus_xfp` unshrunk except `pts_recent` (2 games toward the position mean). |
| Where are injury exits, role changes, abnormal games handled? | `injury_exit_last`/`snap_trend_role` exist but only the participation model uses them; C uses `snap_trend`, so an injury exit is read as a role cut. No game-script or play-by-play evidence. |
| Free context available | Expected counts per component (nflverse ff_opportunity), team plays, final margins, injury reports with timestamps (2019-2024), play-by-play descriptions with in-game injury mentions, weekly rosters with jersey numbers. Not available: routes, coverage, depth-chart intent, pregame betting lines, offensive-coordinator history. |
| Same feature definitions in production and backtests? | Yes: both call `features_for_week`; the live run adds only target construction. |
| Reproducible and comparable? | `select` reproduces `selection.json` exactly (all lambdas, validation MAE, intervals, grid). Artifacts are not versioned per week, so past live forecasts cannot be tied to a specific artifact file. |

Pre-existing test failures (unrelated, not touched): 3 tests in `tests/test_projection_backtest.py`
fail when the machine's local time zone is not UTC (they pass with `TZ=UTC`): the fixture inserts
time-zone-aware datetimes into naive `TIMESTAMP` columns, which DuckDB converts to local time.

## 2. Injury-shortened games (healthy-game usage follow-up)

**How much can two injury-shortened games in the last five reduce projected workload?** On real
forecast rows (2020-2026) with two injury exits among the last five games, the last-5 averages that
feed `ifp-1.0.0` sit a median 24% below healthy-game usage (RB carries x0.76, WR targets x0.75, TE
targets x0.82, QB xFP x0.83); one exit costs about 10%. `snap_trend` adds a further penalty: for
Barkley in week 6 it contributes -3.2 points.

**Is previous-season usage included?** In `ifp-1.0.0`, only `pts_prev_season` (points) and whatever
part of last season falls inside the 5-game window. The challenger adds previous-season healthy-game
usage (`prev_xfpl_pg`, `prev_carries_pg`, `prev_targets_pg`, `prev_snap_pct`, `prev_h_games`,
`prev_team_changed`) and the empirical-Bayes blend `xfpl_eb`.

**Can early exits be identified and discounted with existing data?** Partly. Snap drop + play-by-play
injury mention is high-precision (435 games); snap drop + a new injury listing adds 848 games that are
noisier. The old label's precision against play-by-play evidence is ~20%. Week-18 rest games and
unreported injuries are indistinguishable from role cuts (`role_low` is explicitly ambiguous).

**Full-game vs partial-game opportunity?** Yes via snap share against the player's own reference;
play-by-play also gives the time of his last touch, but snap share already separates the cases and the
touch timing was not added as a feature.

**Player-specific historical workload?** Previously only through 5 games and previous-season points;
now through `xfpl_eb` (current season shrunk toward last season by 3 games) and `prev_*`.

**Appearing vs sustaining a normal workload?** Separated in the inputs (classes, `inj_exits_recent`),
not with a second probability model: prior exits do not predict another exit (2.7% base rate; 2.1-4.7%
after exits, no monotone increase), so P(normal workload | appears) would add a parameter without
evidence.

What each class predicts for the next appearance (median, players with reference snap share >= 40%):

| Position | Class | n | Appeared next week | Next opportunities vs reference |
|---|---|---|---|---|
| RB | full | 3,139 | 85% | 0.97 |
| RB | injury exit (play-by-play) | 59 | 25% | 0.86 |
| RB | injury exit (new listing) | 134 | 62% | 0.73 |
| RB | role_low | 367 | 79% | 0.58 |
| WR | full | 7,705 | 85% | 0.95 |
| WR | injury exit (play-by-play) | 160 | 27% | 0.83 |
| WR | role_low | 946 | 79% | 0.52 |

Interpretation (inferred, not proven): exits mostly preserve the role but carry availability risk and
some limited returns; unexplained drops behave like real role changes.

## 3. Candidates and decisions

Development 2021-22 (13,372 player-weeks), pooled MAE, C = 4.215. Decisions were made here only.

* **Robust loss (Huber).** MAE 4.05-4.13, but bias +0.22 to +0.63 points (under-projection), RMSE
  worse, Start/Sit pair accuracy lower. It wins MAE by estimating something closer to the median.
  `expected_points = p_play x projection` needs a mean, so it is rejected for this estimate. Note: the
  frozen `PROMOTION_CRITERIA` judge MAE alone and would reward this; the confirmation rules here add
  RMSE and bias guards for that reason.
* **Touchdown vs yardage efficiency split, expected league points.** No gain (4.211); the existing
  model already shrinks touchdown booms (examples below).
* **Team volume, last-game volume and margin.** No gain (4.216).
* **Healthy-usage averages** added (4.212) or replacing raw usage (4.209): worse after injury exits.
* **Previous-season usage priors.** Helps team changes; kept in its core form (Q).
* **Game-class flags.** Helps role drops strongly; kept.
* **Empirical-Bayes established workload.** Only single group with a CI below zero; kept. Prior
  strength k in {~0, 1, 3, 6, 10} indistinguishable; k = 3 (pre-set default) kept.
* **Recency weighting.** Half-lives 18/36/72 weeks: small gains; 36 weeks kept with the combination.
* **QB.** No addition improved QB: the challenger's QB model is C (identical predictions).

Frozen: RB/WR/TE = C + flags + established workload + previous-season core, 36-week half-life;
QB = C. Committed in `cc0845b` with `CONFIRMATION_RULES` before the confirmation run.

## 4. Comparison (confirmation 2023-24, run once; exploratory 2025-26)

Tables with every baseline, RMSE, bias and CIs: engine doc, "Confirmation". Summary:

| | Confirmation (n = 13,345) | Exploratory (n = 8,645) |
|---|---|---|
| Pooled MAE C -> ifp-2 | 4.031 -> 4.016 (-0.014 [-0.023, -0.007]) | 4.068 -> 4.050 (-0.018 [-0.028, -0.009]) |
| RB | -0.009 [-0.022, +0.004] | -0.030 [-0.049, -0.010] |
| WR | -0.026 [-0.038, -0.014] | -0.020 [-0.034, -0.008] |
| TE | -0.007 [-0.024, +0.009] | -0.008 [-0.024, +0.005] |
| Pair accuracy C / ifp-2 | 65.45% / 65.57% | 65.15% / 65.32% |
| After role drop | 3.29 -> 3.18 | 3.13 -> 3.04 |
| Team changed | 3.70 -> 3.62 | 3.60 -> 3.52 |
| After injury exit | 4.40 -> 4.42 | 4.41 -> 4.50 |
| Repeated exits | 3.75 -> 3.77 | 4.20 -> 4.39 |
| 80% interval coverage RB/WR/TE | 81.9 / 81.0 / 80.1% | 81.9 / 81.9 / 80.1% |

Relative to the best trailing baseline, RB is still only ~1.6% better (confirmation) and ~0.2%
(exploratory, vs season average): the challenger does not fix the RB weakness of `ifp-1.0.0`.

## 5. Examples (real data; numbers are observed, explanations are inferred)

**Saquon Barkley, 2026 (verified).** Week 1: 71% snaps, 15 carries. Week 2: 16% snaps, 4 carries,
play-by-play injury mention. Week 3: 72%, 15 carries. Week 4: 7% snaps, 2 carries, injured on a play at
3:12 elapsed, which was also his last touch. Week 5: Out (hamstring); Wednesday week-6 report: did not
practice. Both exits are classified `injury_exit`. Last-5 carries 11.0 vs 18.0 over his last five
healthy games. Week-6 pregame (frozen in the ledger): C 6.66, challenger 7.57 points if he plays;
P(appears) 0.29; expected 1.93 vs 2.19. The challenger's largest inputs: `snap_trend` -3.24,
`xfpl_eb` +1.98, `pts_prev_season` +1.38, `last_injury_exit` +1.24. It restores part of his workload,
not all, which is what the cohort evidence supports.

**Established RBs after an injury exit (>= 14 healthy carries per game), 2023-2026, n = 49.** C bias
+1.15 (under-projected), challenger +0.25; MAE 4.71 -> 4.65.

| Player-week | Carries last 5 / healthy | C | ifp-2 | Actual |
|---|---|---|---|---|
| Kenneth Walker III, 2023 wk 10 | 15.8 / 17.6 | 12.7 | 14.3 | 19.7 |
| Christian McCaffrey, 2024 wk 10 | 15.8 / 16.8 | 15.4 | 16.9 | 16.7 |
| Josh Jacobs, 2024 wk 1 | 20.0 / 20.4 | 10.8 | 12.3 | 12.4 |
| Kenneth Walker III, 2023 wk 14 (2 exits) | 13.2 / 17.8 | 9.5 | 11.2 | 9.4 |
| Kimani Vidal, 2025 wk 18 (2 exits) | 12.2 / 17.6 | 6.2 | 7.9 | 2.0 |

**Low-opportunity touchdown booms (>= 12 TD points on <= 6 opportunities), n = 383.** Both models
already discount them (MAE C 6.14, ifp-2 6.12). Josh Reynolds 2023 wk 3: 23.6 points on 6
opportunities, both projected ~10, actual 0. Noah Gray 2024 wk 13: 22.6 on 4 targets, both ~6.5,
actual 9.8.

**Ambiguous "role cut".** Justin Jefferson 2023 wk 1: his last game (2022 week 18) was a snap drop with
no injury evidence and no blowout, so it is `role_low`; it was most likely rest. The challenger still
projected 15.2 (C 14.4); actual 24.0. Week-18 rest is a known false role signal.

## 6. Weekly pipeline (deliverable) and prospective monitoring

Commands, timing and safety rules: engine doc, "Weekly cycle". Verified on real data on 2026-10-15:
`status --week 6` ready (weeks 1-5: 32/32/32/32/30 teams with box scores and snaps, xFP 76-77%);
`train --week 6` produced champion `7dfbc53374e5c8ce` (bit-identical to the production artifact) and
challenger `af1b5ca885e6c22e`; a second `train` reused both ids; `predict --week 6` froze 436 players
(413 with a challenger estimate); a second `predict` was a no-op; a `train` dated after Thursday's
kickoff was refused. Retrospective replays of weeks 3-5 (labeled, not evidence): challenger - champion
MAE -0.023 [-0.030, -0.011] on 1,177 appearances.

## 7. Data quality

* nflverse expected points are missing for ~23% of appearances (snap-only games); imputed with an
  indicator, never zero.
* 2025 and 2026 injury rows have no `date_modified`, so the pre-kickoff filter cannot be applied to them.
* Play-by-play injury mentions depend on the gamebook writer; injuries reported after the game are
  invisible to it, hence the report-based fallback.
* nflverse expected-points models are fitted by nflverse on historical seasons, so early seasons are
  partly in-sample for that upstream model (affects both C and the challenger).

## 8. Before any promotion

1. Accumulate frozen pregame forecasts with `weekly_learning.py cycle` every week before kickoff.
2. `SHADOW_PROMOTION_CRITERIA` (>= 8 weeks, >= 400 appearances per position, CI vs champion below 0,
   bias and starters guards, coverage 75-85%) -> `eligible_for_review`.
3. The existing `PROMOTION_CRITERIA` (3% vs best trailing baseline, ESPN rule) for any consumer use.
4. Your explicit approval. Nothing in this branch changes lineups, start/sit, trades, waivers or the API.

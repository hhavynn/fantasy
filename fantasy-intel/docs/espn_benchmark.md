# Prospective ESPN benchmark

Read-only research tooling that answers one question: on exactly the same completed player-weeks, is the
production independent model (`ifp-1.0.0`) closer to the league points than ESPN's own pregame projection?
It never promotes a model, changes a projection, writes the database, trains, or syncs. ESPN stays the default
source. Nothing imports it (`app/analytics/espn_benchmark.py`, `scripts/espn_benchmark.py`,
`tests/test_espn_benchmark.py`).

`scripts/refresh_all.py` runs `record` for the latest fully final week at the end of every refresh
(see "Weekly evidence routine (automated)" in `independent_projection_engine.md`); `--skip-evidence` turns it off.

```
cd backend
uv run python ../scripts/espn_benchmark.py score  --season 2026 [--weeks 1-6] [--json]
uv run python ../scripts/espn_benchmark.py record --season 2026 --week 6
```

* `score` opens the DuckDB file read-only and prints the scorecard (a table, or the full JSON with `--json`).
* `record` scores weeks 1..W and writes the scorecard once, to `--out` (default `data/models/espn_benchmark`,
  git-ignored via `data/models/`), named `s<season>w<week>-<sha256 of the file bytes, 16 hex>.json`.
  Identical inputs give the same name and nothing is written; any change (a newly final game, a new run, a
  new ESPN snapshot) gives a NEW file; an existing file is never overwritten, and a damaged file at the
  target name is reported, not replaced. The scorecard contains no wall-clock value, so it is reproducible.
* Options: `--resamples` (1000), `--seed` (20261014), `--min-weeks` (4), `--horizon-window` (24 h).

## Evaluation rows

`evaluation_rows(con, season, weeks, forecast_reader=..., ...)` returns one row per
`(season, week, espn_player_id)` known to ESPN history or to the forecast reader, with exactly these keys
(CONTRACTS section 6):

`season, week, gsis_id, espn_player_id, position, team, kickoff, outcome_status, actual_points, espn_pred,
espn_captured_at, espn_horizon_h, ours_independent_pred (expected_points), ours_conditional_pred
(projected_points), ours_origin (ours | ours_zero | espn), ours_consumer_pred, run_id, run_created_at,
ours_horizon_h, exclusion_reason`.

Every row that must not be scored has one `exclusion_reason`; the first failing check wins, in this order:

| reason | meaning |
|---|---|
| `unsupported_position` | not QB/RB/WR/TE (K, D/ST have no model) |
| `uncertain_id_mapping` | no high-confidence ESPN->GSIS crosswalk row, flagged "conflict", or two ESPN ids for one GSIS id. Never guessed |
| `team_unknown` | no team from the forecast or the weekly roster |
| `bye` / `kickoff_unknown` | the team has no scheduled game that week / its kickoff time is ambiguous |
| `no_espn_snapshot` / `espn_not_pregame` | no ESPN snapshot with a projection / none strictly before the player's own kickoff |
| `no_independent_forecast_for_week` | the reader returned no pregame forecast for any player that week (the model was not live) |
| `independent_not_pregame` | run created at or after the player's own kickoff (or flagged not pregame) |
| `independent_model_mismatch`, `independent_scoring_mismatch` | other model version / league-scoring fingerprint |
| `independent_artifact_time_unknown`, `independent_artifact_after_first_kickoff` | artifact fit time unknown / not strictly before the week's FIRST kickoff (`promotion_tracker.eligible_runs` rule) |
| `independent_id_mismatch` | the forecast row names another GSIS id than the crosswalk |
| `outcome_pending_game`, `outcome_missing_ingestion`, `outcome_bye`, `outcome_unresolved_identity` | `weekly_learning.label_outcomes` status; never scored, never a zero |
| `week_not_fully_final` | the row is resolved, but its NFL week is not fully final (`weekly_learning.week_finality`: every scheduled game final and every team's box scores and snaps synced). Reported under `preliminary`, never evidence |

Time rules: all stored timestamps are naive UTC. A forecast is pregame only if its information time is strictly
before the kickoff of the player's OWN game (`projection_backtest._kickoff`, Eastern to UTC); Thursday and
Sunday players are judged against their own kickoffs. The team is the forecast's `nfl_team`, else the weekly
roster team. If the forecast and the current schedule disagree on the kickoff, the earlier one is used.
Horizon = hours from the information time (ESPN `synced_at`, run `created_at`) to kickoff.

Reader rows are re-verified here even though the reader should only return eligible ones (defence in depth
against temporal leakage).

## Origins (as `projection_source.weekly`)

* `ours`: coverage `projected` with `expected_points` (p_play x points-if-plays).
* `ours_zero`: coverage `bye` or `withheld_unavailable`: 0.0, as ESPN shows for a player who cannot play.
* `espn`: everything else (no row, abstain, no participation estimate, ...): the consumer sees ESPN's number.
  The model gets no credit: `ours_independent_pred` and `ours_conditional_pred` are null and
  `ours_consumer_pred = espn_pred`. The constants are copied from `projection_source` (a test keeps them in
  sync); this module must not import it.

## Comparisons (each on an equal population)

Both predictions and the actual exist for every scored row; `assert_equal_population` enforces it.

| comparison | population | ours column |
|---|---|---|
| `pure_independent` | origin `ours`/`ours_zero`, APPEARED + CONFIRMED_ABSENT (absent = 0). Fallback rows are excluded and counted | `ours_independent_pred` |
| `consumer_visible` | every eligible resolved row, fallback included (zero difference, no credit); reports `fallback_share` | `ours_consumer_pred` |
| `conditional` | APPEARED only, origin `ours` | `ours_conditional_pred` |
| `unconditional` | APPEARED + CONFIRMED_ABSENT, origin `ours` with a points-if-he-plays estimate: exactly the `conditional` rows plus the confirmed absences | `ours_independent_pred` |
| `horizon_matched` | `pure_independent` rows with abs(ours horizon - ESPN horizon) <= window; reports how much of `pure_independent` it keeps | `ours_independent_pred` |

Metrics per position and `ALL`: `n`, `n_model` (rows that are not fallback), `weeks_covered`, MAE / RMSE / bias
for ours and ESPN (`forecast_backtest.metrics`; bias = mean(actual - prediction)), `mae_diff` = MAE(ours) -
MAE(ESPN) (negative: ours closer) with a seeded 95% bootstrap CI that resamples whole NFL weeks
(`forecast_backtest.week_bootstrap_mae_diff`; player-weeks of a week are correlated, so a row-level bootstrap
would be too narrow).

## Evidence gate

Only FULLY FINAL NFL weeks count (`weekly_learning.week_finality`, the rule `weekly_learning.evaluate` uses):
`weeks_covered`, the bootstrap and every verdict ignore partial weeks. Their resolved rows are excluded as
`week_not_fully_final` and reported in the scorecard's `preliminary` block, descriptive only, with no verdict.
`fully_final_weeks` / `partial_weeks` list them.

Each position gets a `verdict` in `{insufficient_evidence, no_significant_difference, ours_better, espn_better}`.
It is `insufficient_evidence` unless the model-covered rows reach
`PROMOTION_CRITERIA["espn_min_matched_per_position"]` (read at call time, 300 today) AND span at least
`min_weeks` distinct fully final weeks (default 4). `ours_better` / `espn_better` only when the 95% week-clustered
CI of MAE(ours) - MAE(ESPN) excludes 0; the decision uses the unrounded bounds (displayed values are rounded).
Fallback rows never count toward sufficiency.

The comparison-level `verdict` summarises the per-position verdicts (`position_verdicts`): `insufficient_evidence`
unless at least one position clears the gate; `ours_better` / `espn_better` only when every position that
clears it agrees; otherwise `no_significant_difference`. The pooled `ALL` row keeps its numbers but carries only a
`descriptive_verdict`: pooling positions is not judged against a per-position bar.

## Diagnostics

`coverage`: rows, scored, counts by `exclusion_reason` and `outcome_status`, scored rows by origin, and the same
per week and position. `reader_exclusions`: the reader's run-level (and aggregated per-player) drops with
reasons.

## Forecast readers

`forecast_reader(con, season, weeks, *, model_version, scoring_fingerprint) -> (rows, exclusions)` is the
contract of `production_forecasts.pregame_forecasts` (CONTRACTS section 5). Rows carry `season, week,
espn_player_id, gsis_id, position, run_id, run_created_at, model_version, artifact_id, artifact_created_at,
scoring_fingerprint, nfl_team, kickoff, pregame, coverage_status, projected_points, p_play, expected_points`.

**Integration with the immutable store is complete.** The default reader is
`production_forecasts.pregame_forecasts` (schema v8, `independent_projection_rows`, written by
`independent_projection_store.write_run`): per player-week, the row of the LATEST eligible run created strictly
before that player's own kickoff, so a rerun after kickoff is never credited and an earlier pre-kickoff run is not
lost when a later one exists. The scorecard says where forecasts came from in `forecast_source`:

* `production_forecasts`: the run-keyed table exists (every schema v8 database). The frozen legacy table is not read.
* `legacy`: only a pre-v8 database (no `independent_projection_rows`) falls back to `legacy_forecast_reader`, which
  reads `independent_projection_runs` + `independent_player_projections` and applies the kickoff and artifact checks
  itself. The legacy table keeps one row per player-week and a later `project --week N` replaced the rows of an
  earlier run, so only the surviving run's forecast is recoverable: coverage is a lower bound.
* `injected`: a `forecast_reader` passed by the caller.

The reader's run-level exclusions (other model or scoring, artifact fitted after the first kickoff, integrity
failures, runs whose rows were destroyed by the pre-v8 overwrite, runs created after every player's kickoff) are
passed through verbatim as `reader_exclusions` and counted by reason in
`coverage.forecast_runs_excluded_by_reason`. `evaluation_rows` still re-verifies every returned row (pregame
against the player's own kickoff, model, scoring, artifact before the first kickoff, crosswalk id): defence in
depth that rows from `pregame_forecasts` pass as delivered.

## Reading the result honestly

Different information times (a Tuesday run vs a Saturday ESPN snapshot) are visible in the horizon fields and
in `horizon_matched`. A comparison with few weeks is `insufficient_evidence` however good it looks. The label is
league points from nflverse box scores under the league's current scoring; a scoring change makes older runs
ineligible (`independent_scoring_mismatch`), and an unsupported league scoring makes the whole scorecard
`available: false`.

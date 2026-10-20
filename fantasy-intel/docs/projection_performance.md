# Projection performance scorecard

The `/projection-performance` page answers one question honestly, per position: on fully final NFL weeks, were
Fantasy Intel's production projections closer to the league points than ESPN's pregame projections, and did they
make better Start/Sit decisions? Until the evidence says so, the page says "Insufficient prospective evidence." It
adds no forecasting algorithm and never promotes, retunes or rolls back a model.

## Why a published report

The statistics come from research code: the prospective ESPN comparison (`app/analytics/espn_benchmark.py`, see
[espn_benchmark.md](espn_benchmark.md)) and the Start/Sit decision evaluation
([start_sit_evaluation.md](start_sit_evaluation.md)). Both need polars and numpy, which the hosted Vercel API does
not ship, and nothing in `app/` other than the benchmark itself may reference the benchmark
(`test_app_modules_never_reference_the_benchmark`). So the scorecard is computed offline and published:

```
refresh_all.py
  sync_espn -> sync_nfl -> compute_analytics
  -> projection_performance.py publish     build from a read-only connection, then ONE INSERT (write-once row)
  -> publish_motherduck.py                 every table copied in one transaction
hosted API  GET /api/projections/performance   app/analytics/performance_report.py (SQL + stdlib only)
page        /projection-performance
```

* **Versioned:** `schema_version` `projection-performance/v1`; the reader serves only versions it knows.
* **Integrity-checked:** the row stores the canonical JSON text and the sha256 of exactly those bytes
  (`report_sha256`). The reader recomputes the hash on every request and also re-checks the claims the page relies
  on: four positions, known verdicts, statement matching verdict, a directional verdict only with an interval on one
  side of 0 (the same checks for every RB volume group when present), no identifier fields, not synthetic. A row that fails any check is listed under `rejected` with its
  reason and the newest row that verifies is served instead. If none verifies, the API answers
  `available: false, status: integrity_failed` and the page shows no figures.
* **Safe against partial publication:** the report is one row inserted by one statement (`ON CONFLICT DO NOTHING`,
  never updated or deleted), and `publish_motherduck.py` replaces every hosted table in one transaction, so the
  hosted copy holds either the old or the new report, never part of one. `store_report` refuses a payload that would
  not verify, so a bad build can never become the newest row.
* **Stale reports:** `status: stale` with reasons when an ESPN or NFL sync finished after the report's `data_as_of`
  (a manual sync without a rebuild), or the report is more than 8 days old. Stale reports are still shown, under a
  caution notice.
* **Synthetic data:** a payload marked `synthetic: true` is rejected unless the API process sets
  `PROJECTION_PERFORMANCE_ALLOW_SYNTHETIC=1` (local screenshots only, never on the hosted deployment), and the page
  then labels it at the top. Synthetic data lives only in tests and
  `frontend/lib/fixtures/projection-performance.synthetic.json`.

```
cd backend
uv run python ../scripts/projection_performance.py show    --season 2026 [--json]   # build and print, writes nothing
uv run python ../scripts/projection_performance.py publish --season 2026            # build and store one row
```

`refresh_all.py --skip-report` skips the step. A failure is a `WARNING: report:` line; the previous report stays
published and the refresh exit code is unchanged.

## Weekly replay (hindsight, not evidence)

The scorecard grows one week at a time and only for the model that was live that week, so a model change takes weeks
to show up there. The **Weekly replay** section answers the faster question: if today's model had made every finished
week of the season, how close would it have been, and is that better or worse than the previous model, week by week?

```
refresh_all.py  ... -> projection_performance.py publish -> model_replay.py publish -> publish_motherduck.py
hosted API      GET /api/projections/replay        app/analytics/model_replay.py (SQL + stdlib only)
page            /projection-performance            "Weekly replay" section, above the ESPN comparison
```

* **How:** for every fully final week W of the season, the production model (context_model, its SHIPPED components),
  the previous version (ifp-2.1.0's frozen spec) and the recent-average baseline are refitted on rows of weeks
  strictly before W (the walk-forward of `scripts/ifp22_backtest.py`, reusing its cached store) and predict W. MAE per
  week and position on identical player-weeks.
* **Which models:** the replay follows `app/analytics/context_model.py` (ifp-2.2.0, its SHIPPED components) and
  replays `app/analytics/injury_model.py`'s spec (ifp-2.1.0) as the prior version. Whoever ships the next ifp version
  updates both in `scripts/model_replay.py` (`replay` and `PRIOR_VERSION`).
* **Snapshots:** `publish` stores one write-once row of `model_replays` (canonical JSON + sha256, like the report)
  unless the newest row has the same model fingerprint (version, config and the source of every module the replay
  loaded), data fingerprint (store rows and features, QB start candidates, the store's source provenance) AND output
  fingerprint (every replayed forecast and outcome). A change the first two miss still shows in the output and stores
  a new snapshot; only the model fingerprint decides which snapshot counts as "the previous model".
* **Comparison:** the page shows the newest snapshot's MAE per week with the change against the previous model: the
  newest earlier snapshot of a different model fingerprint whose forecasts differ on a shared week (per-week output
  fingerprints), so an edit to a module that does not change any forecast never replaces the real previous model.
  Before the first model change it compares against ifp-2.1.0 replayed on the same rows. A change under 0.05 points
  reads as about the same; this is one season, not a significance test.
* **Data drift:** each week also carries an input fingerprint (every store row and QB candidate up to that week). A week
  whose inputs changed between the two snapshots (stat corrections, re-synced depth or injury data) is marked † on the
  page; its change mixes model and data, so the summary, the tally and the season row leave it out.
* **Limits:** hindsight. Inputs are today's data (stat corrections, depth charts and injury reports as synced now),
  outcomes are conditional on playing (players with at least one recent game), and nothing here is what the site
  showed that week. It never feeds a verdict, a promotion or the scorecard. ESPN is deliberately not part of it.

```
cd backend
uv run python ../scripts/model_replay.py show       # replay and print, writes nothing (~1-2 min)
uv run python ../scripts/model_replay.py publish    # store a snapshot if the model or weeks changed
uv run python ../scripts/model_replay.py history    # stored snapshots and the week-by-week comparison
```

The season row compares both models over the weeks both replays cover (labelled with those weeks when the newer
replay has more). `refresh_all.py --skip-replay` skips the replay step; `--skip-report` no longer affects it.

## What the report contains (aggregates only)

No player, roster, league or fantasy-team identifier is ever published; only counts and error statistics.

| Block | Source | Notes |
|---|---|---|
| `positions` | benchmark `pure_independent` (headline), `consumer_visible`, `conditional`, `lead_times` | Per QB/RB/WR/TE: production model version, fully final weeks, paired n, ESPN and ours MAE / RMSE / mean signed error (`bias` = mean(actual - projection)), MAE(ours) - MAE(ESPN) with the 95% week-clustered interval, verdict and its one-sentence statement, coverage and ESPN-fallback share, lead times, excluded observations by reason |
| `comparisons` | the scorecard's comparisons, unchanged | `pure_independent`, `consumer_visible`, `conditional`, `unconditional`, `horizon_matched` |
| `weekly` | `espn_benchmark.weekly_errors` | MAE per fully final week and position on the headline population; the build refuses to publish if the weekly n do not add up to the position's paired n |
| `rb_volume_split` | `espn_benchmark.rb_volume_split` on the scorecard's own rows | RB accuracy split by pregame volume (carries + targets per game over the back's last <= 5 games of the season before the week): `low_volume` (< 4), `volume` (>= 4), `no_recent_games`. For `pure_independent` (headline) and `conditional`, each group has the same statistics, evidence gate and statement as a position; the build refuses to publish if the groups do not add up to the RB rows. Why: `docs/research/rb_projection_diagnostics.md` (the model loses to a season average on low-volume backs and wins with volume, so the pooled RB number mixes two effects). Optional for the reader: reports built before the split still verify |
| `preliminary` | scorecard `preliminary` | Resolved rows of weeks that are not fully final: descriptive, never a verdict |
| `model_versions` | `espn_benchmark.version_comparison`, `projection_source.select_run` | ifp-2.0.0 vs ifp-1.0.0 only on player-weeks where BOTH have a valid pregame forecast (each version read by its own reader rules; reconstructed or legacy-destroyed runs never pass them); per-week pregame counts per version, never pooled; what the site shows now for each week |
| `start_sit` | `start_sit_evaluation.evaluate_sets` on recorded pregame decision sets | Pregame-roster population only; one model set per production version (`ifp2_production`, `ifp1_production`), each with ESPN and A1 |
| `coverage` | scorecard coverage | Exclusions and outcomes by reason, excluded forecast runs |

The scorecard's own content hash (`benchmark.content_sha256`) is recorded, so a report can be tied to the
`espn_benchmark.py score --json` output of the same database.

## Statistical rules (unchanged from the benchmark)

* Only forecasts made strictly before each player's own kickoff count; a forecast made or rerun after kickoff is
  never scored, and nothing is reconstructed.
* Only fully final NFL weeks are evidence (`weekly_learning.week_finality`).
* The headline is **unconditional**: every resolved player-week the model projected, a player who did not play
  scores 0. The **conditional** comparison (players who appeared) is reported separately and never as the headline.
* Paired: both projections and the outcome exist for every scored row. Players the model could not project show
  ESPN's number on the site; those fallback rows are counted (`fallback_share`) but never credited to the model.
* Per-position verdict: `insufficient_evidence` below 4 fully final weeks or 300 matched observations; otherwise
  `no_significant_difference` when the 95% week-clustered interval of MAE(ours) - MAE(ESPN) includes 0, else
  `ours_better` or `espn_better`, stated equally plainly. The minimum is a floor, not a promise of a difference.
* Start/Sit: at least 8 weeks and 200 decisions before any verdict. A decision counts only when ESPN, A1 and the
  model each projected every player in it before kickoff.
* The owner override that made ifp-2.0.0 production is not statistical validation; the page says so.

## Forecast horizon (rest-of-season runs)

Since the rest-of-season cycle (#80) stores a run for every remaining week, a week can have runs at several forecast
horizons (`forecast_horizon` = target week - the week the cycle ran for; NULL reads 0). Only horizon 0, the week's own
pregame forecast, is evidence: `production_forecasts.pregame_forecasts` (and both legacy readers) exclude every
horizon > 0 run with the reason "rest-of-season forecast (horizon k), not evidence", so the scorecard, its paired
observations, coverage, lead times and verdict gates never include one; such runs appear only in the excluded-runs
list. Longer-horizon accuracy is not reported. The consumer view (`select_run`) may still serve a rest-of-season run
for a week without a newer one; the model-version table says so per week (`forecast_horizon`, "not scored").

## Start/Sit: production forecasts in decision sets

`scripts/start_sit_evaluation.py` now attaches the production forecasts of BOTH versions to each recorded decision
set, and `DEFAULT_MODEL_SETS` gains `ifp2_production`:

* `ifp2_production` (the production model) is exactly the run the site served for the week
  (`projection_source.select_run`, the consumer rule), credited only when that run is ifp-2.0.0 at horizon 0 and an
  eligible evidence run (`production_forecasts.eligible_run_ids`). A served rest-of-season run, the ifp-1 fallback or
  ESPN gives no value, so those decisions are never credited to the production model
  (`served_production_rows`; the reason is kept in the set's `meta.attached.ifp2_production_exclusions`).
* `ifp1_production` (the fallback model) is its own horizon-0 pregame forecasts (`pregame_forecasts`), evaluated as a
  separate model set, never pooled with ifp-2.
* A retrospective `report` on roster snapshots asks `select_run` what it serves NOW; a run made after a player's
  kickoff is refused per player, so a later rerun can hide an earlier served forecast there. Recorded decision sets
  (what the page uses) capture the served run before kickoff.
Before this change only ifp-1 production and the research ledger were recorded, so the model the site actually
serves could never have been evaluated on real decisions. Sets recorded before the change simply lack the
`ifp2_production` model (unavailable, never backfilled).

## Real data (2026-10-16)

At the time of writing there are zero recorded ESPN benchmark scorecards and zero Start/Sit decision sets, week 6
is not final, the week 6 ifp-2 run was created after Thursday's kickoff (only the later games' rows are pregame),
and week 7 serves the ifp-1 fallback. The page therefore shows insufficient prospective evidence for every
position and an empty Start/Sit section. That is the correct state.

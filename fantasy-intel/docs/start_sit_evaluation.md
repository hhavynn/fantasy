# Real-roster Start/Sit decision evaluation

Workstream E. Research and diagnostics only: it changes no lineup, projection, model or table. Code:
`backend/app/analytics/start_sit_evaluation.py` (decision layer + SQL helpers),
`scripts/start_sit_evaluation.py` (adapters for outcomes, baselines, ledger; `report` and `record`),
tests `backend/tests/test_start_sit_evaluation.py`.

The question: on the rosters people actually hold, do our projections make better lineup decisions than
ESPN and than simple baselines? The 64.7% (context model) vs 64.6% (recent average) held-out pair accuracy
quoted elsewhere is a comparison between two of OUR models on 1,012 same-roster pairs
(`forecast_backtest.pair_decisions` restricted by `league_roster_pairs`). It says nothing about ESPN and is
not an edge; the report repeats that note at the top.

## 1. Audit

| Source | What it holds | Consequence for evaluation |
|---|---|---|
| `lineup_snapshots` (PK league, season, period, team, player) | Every fantasy team's roster, slot, `is_starter`, points, per scoring period. `replace_where` per period on every ESPN sync; `synced_at` is the sync time. | Contemporaneous only for a row synced before kickoff. The refresh runs weekly (launchd), so a completed week's snapshot is written after its games: the roster (and the lineup the manager finally set) is "roster at snapshot", NOT provably pregame. Only the current period, synced before kickoff, qualifies. Each team-week is labeled `pregame_snapshot` (every row synced strictly before its player's kickoff) or `post_hoc_snapshot`, and both are counted separately. |
| `leagues` | `roster_slots` JSON (`QB`, `RB`, ..., `FLEX`, `BE`, `IR`) and `scoring_items`. | Slot counts drive the lineup optimiser and which cross-position pairs are real decisions. A league whose scoring fingerprint differs from the one the outcomes use is skipped and listed (CONTRACTS section 3). |
| `analytics/lineup.py` | `lineup_slots`, `assign_lineup` (fixed slots first, exact search over flex slots), `eligible`, `FLEX_ELIGIBILITY`. | Reused unchanged: the same optimiser scores the model's lineup and the hindsight lineup. K and D/ST slots are dropped (no model projects them). |
| `start_sit.py` | The product feature (ESPN default, opt-in source switch). | Not touched. |
| `forecast_backtest.league_roster_pairs / pair_decisions / PAIR_POOL / CLOSE_PAIR_POINTS` | Same-position pairs from snapshots; accuracy and regret on the top-N pool by A1. | Semantics copied (ties, half credit, close = A1 within 3 points). Findings: `league_roster_pairs` groups by `team_id` without `league_id`, so a database with several leagues can pair players that never shared a roster; it does not check whether the snapshot was pregame; pairs include two players who both start (no real decision); and `pair_decisions` scores conditionally (players who appeared), unlike a real lineup where a sitter scores 0. The independent review confirmed the cross-league grouping (`league_id` is available but neither selected nor grouped); it is NOT changed here and is being handled separately, so the 1,012-pair figure should be read with that caveat. |
| `weekly_learning.frozen_rows` | Research ledger rows (ifp-1 refit champion, ifp-2 challenger) created strictly before kickoff. | Used for `ifp1_frozen` / `ifp2_frozen`, unconditional `expected_points`, `captured_at` = the run's creation time. |
| `forecast_backtest.espn_pregame` | Latest ESPN snapshot before the player's kickoff, keyed by GSIS. | Re-implemented here keyed by ESPN id so the capture time is kept for the pregame check (the benchmark's `evaluation_rows` will own this). |
| A1 / A2 | `baseline_predictions(features_for_week)`: recent and season average from games strictly before the week. | Point-in-time by construction (`point_in_time` flag); no capture time needed. |
| Production ifp-1 | `independent_player_projections` (one run per week, replaced by the next run). | `legacy_forecast_reader` returns a row only if the run was created strictly before the player's kickoff, the model version and scoring fingerprint match and the artifact predates the week's first kickoff. Same signature as CONTRACTS section 5. |

## 2. Decisions and rules

Input per roster player (keys follow CONTRACTS section 6): `season, week, gsis_id, espn_player_id, position,
team, kickoff, outcome_status, actual_points`, plus `league_id, fantasy_team_id, slot, is_starter,
roster_synced_at` and `preds[model] = {value, captured_at, point_in_time}`.

* Pregame: a prediction counts only if `captured_at < kickoff` of the player's own game (strict) or it is a
  point-in-time baseline. Unknown kickoff or capture time is never pregame. A prediction at or after
  kickoff is excluded and counted as `post_kickoff_prediction:<model>`.
* Outcomes: `appeared` scores league points, `confirmed_absent` scores 0 (these are unconditional
  decisions, like a real lineup), everything else (`pending_game`, `missing_ingestion`, `bye`,
  `unresolved_identity`) is excluded and counted. Identity needs a high-confidence ESPN to GSIS mapping
  without a conflict note.
* Equal population: a decision is scored only when EVERY compared model has a valid pregame prediction for
  EVERY involved player. Model sets (`baselines` = espn, A1, A2; `ifp1_production`, `ifp1_frozen`,
  `ifp2_frozen` each with espn and A1) are separate populations, so a sparse model never shrinks the
  baselines. A set with a model that has no pregame evidence anywhere is reported as unavailable.
* a. Same-position pairs on one roster: pick the higher projection; accuracy and regret as in
  `pair_decisions` (actual ties skipped and counted; projection ties count half). `contested` marks pairs
  where the roster holds more players of that position than dedicated slots.
* b. Flex pairs: different positions, only when the league has a flex slot both can fill (an RB never fills
  WR or WR/TE).
* c. Lineups: skill slots only. Model lineup vs hindsight lineup, regret in points (mean, p90, max); points
  won or lost against the ESPN-recommended lineup; and, as reference only, against the lineup the manager
  set (a completed week's snapshot already holds the final lineup, so it is not a pregame decision).
  Policy `complete` (default) drops a lineup if any live roster player is unusable; `valid_subset` scores the
  usable players and reports `dropped_players`. Whether a lineup can be filled is decided from positions and
  slots alone (never from scores or predictions), and the optimiser is fed values shifted to be at least 1
  (a constant per slot, so the optimal set is unchanged) because `lineup.assign_lineup` starts its flex
  search from -1.0 and would otherwise leave a slot empty when the best flex sum is below that. Points are
  always summed from the real actual scores.
* d. Close calls: A1 within 3 points, A1 being a pregame quantity identical for every model.

Statistics: seeded bootstrap that resamples whole (season, week) clusters, for differences vs ESPN and vs A1
(accuracy, mean regret, lineup regret, lineup points). Verdicts: `insufficient_evidence` below
`min_decisions` (200) or `min_weeks` (8); `no_significant_difference` if the 95% CI covers 0; else `better`
or `worse` by the metric's direction. The same real player pair on several rosters counts once per roster
(reported as `unique_player_pairs`); week clustering covers that correlation.

Populations: every model set is reported twice and labeled. The headline is POOLED (all roster snapshots,
pregame and post-hoc). `pregame_roster_only` repeats the whole evaluation, with its own counts, intervals and
verdicts, on team-weeks whose every roster row was synced before its player's kickoff. Read the verdict of
the population you mean; the pooled one carries the post-hoc roster caveat. Default sets: `baselines`,
`ifp_head_to_head` (espn, A1, ifp1_frozen, ifp2_frozen on one population: ifp-2 against the champion),
`ifp1_production`, `ifp1_frozen`, `ifp2_frozen`.

## 3. Using it

```
cd backend
uv run python ../scripts/start_sit_evaluation.py report --season 2026 [--weeks 1-9] [--source both] [--json]
uv run python ../scripts/start_sit_evaluation.py record --season 2026 --week 7 [--out DIR]
```

`report` opens databases read-only. `record` freezes a decision set (rosters at the pregame snapshot plus every
model's pregame prediction and capture time, no outcomes) as
`OUT/decision_sets/s<season>w<week>-<content_sha16>.json`, default `data/models/start_sit` (git-ignored).
The file is written exclusively: recording identical content again (whenever) returns the existing file.
`recorded_at` is the REAL wall-clock time and sits inside the file hash `sha`, so editing it fails
verification; `content_sha` leaves it out, which is what makes a re-record idempotent. Players whose game
already kicked off (or whose kickoff is unknown) are refused, as is any roster row synced after the cutoff,
and predictions captured after it are removed; nothing is written if no player remains (exit 2).
`--now` may only move the evaluation time into the past: a `--now` earlier than the real clock produces a set
tagged `audit_only` (with `meta.clock_override`) that the `recorded` source ignores and counts as
`audit_only_sets_ignored`; a `--now` in the future is refused. Run it once per week before the first kickoff,
after the ESPN and weekly-learning refreshes.

Scoring recorded sets: audit-only sets are ignored. Per (league, fantasy team, week) the roster is the one in
the LATEST usable set that covers the team, so a player dropped between two sets is gone (no phantom roster).
An earlier row survives only for a player the later set refused as `kicked_off` (refusals carry league and
team for this). A row is used only if its set was recorded before the player's kickoff and holds no roster
sync or prediction from after its own recording. A tampered file is ignored. Outcomes are joined at report
time.

## 4. Expected real-data coverage

No real database is available to the author, so these are structural expectations, to be confirmed with
`report` on the live database:

* ESPN and A1/A2 decisions exist wherever a snapshot of `espn_projection_history` predates kickoff; the ESPN
  sync freezes them at kickoff, so that table is the limiting source.
* Retrospective rosters are post-hoc for every completed week. Decisions are still comparable across models
  (all predictions are pregame) but carry the snapshot caveat; only the headline `pregame_roster_only`
  counts are free of it, and that will be ~0 until `record` has been running.
* ifp-1 production: the table keeps only the latest run per week, so earlier forecasts are mostly gone
  (Workstream A's immutable store fixes this). ifp-1/ifp-2 frozen: only weeks in the ledger. Expect these
  sets unavailable or tiny now.
* Prospective: with N leagues of ~12 teams, each recorded week yields on the order of ten lineups and
  ~50-100 same-position pairs per league. The decision threshold (200) is reachable in weeks; the week
  threshold (8) is not before eight recorded weeks, i.e. no verdict from `record` alone before about week 15
  if recording starts at week 7. Until then the answer is `insufficient_evidence`.

## 5. Integration with the benchmark and the production store

Workstream B's `evaluation_rows` (CONTRACTS section 6) carries the same per player-week keys. When it merges,
replace `espn_pregame_predictions` and the ledger/baseline attachments in the script with its rows
(`ours_independent_pred`, `ours_conditional_pred`, `espn_pred`, `espn_captured_at`); the decision layer
needs no change because a candidate only requires `{value, captured_at}` per model. Workstream A's
`production_forecasts.pregame_forecasts` replaces `legacy_forecast_reader` (same signature, injected through
`forecast_reader`). Nothing from either branch is imported here.

## 6. Limits

K and D/ST are not modelled. Roster and slot settings are per league as synced; mid-week transactions and
lineup locks are not reconstructed. League points come from nflverse under the league scoring fingerprint
(contract section 4), not from ESPN's `points` column. Decisions on a real roster are few and correlated;
no result here is a claim until the verdict says so.

# Independent Weekly Projection Engine (V1, research)

An NFL-evidence fantasy-point model for QB/RB/WR/TE that **never uses ESPN projections as an
input**. ESPN appears only as an external comparison. It is research output: nothing in lineups,
trades, waivers, playoff odds, player scores or market signals reads it.

**Current status (model `ifp-1.0.0`, held-out evaluation on real data, see "Results"):**

| Position | Status | Start/Sit ready |
|---|---|---|
| QB | `validated` vs trailing-average baselines only (not vs ESPN) | no |
| RB | `experimental` (no better than season average) | no |
| WR | `experimental` (2.8% better than baseline; below the 3% bar) | no |
| TE | `experimental` (2.7% better than baseline; below the 3% bar) | no |
| K, D/ST | `unsupported` (no independent model in V1) | no |

No position is Start/Sit ready: there are **zero** completed player-weeks with a pregame ESPN
snapshot, so no claim of beating ESPN is possible yet.

## Files

| File | Role |
|---|---|
| `backend/app/analytics/league_scoring.py` | ESPN `scoring_items` -> weights on nflverse box-score columns; refuses unsupported rules |
| `backend/app/nfl/history.py`, `scripts/sync_nfl_history.py` | Optional finished-season nflverse ingestion into a separate DB |
| `backend/app/analytics/forecast_features.py` | Point-in-time panel + features |
| `backend/app/analytics/independent_projections.py` | Baselines, ridge models, artifact, explanations |
| `backend/app/analytics/forecast_backtest.py` | Walk-forward validation, selection, held-out evaluation, ESPN matching |
| `backend/app/analytics/independent_projection_store.py` | Live week-W run with availability/ID guards |
| `backend/app/api/independent_projections.py` | Read-only `GET /api/projections/independent` |
| `scripts/backtest_independent_projections.py` | `audit`, `select`, `evaluate`, `fit`, `project` |

Added dependency: **numpy** (closed-form ridge, bootstrap). No scikit-learn, no gradient boosting,
no cloud services, no LLM numbers.

## Runbook

From `fantasy-intel/backend` (all outputs land in git-ignored `data/`):

```bash
uv run python ../scripts/sync_nfl_history.py --seasons 2019-2025                 # ~20 s, ~60 MB, separate DB
uv run python ../scripts/backtest_independent_projections.py audit
uv run python ../scripts/backtest_independent_projections.py select              # validation seasons only
uv run python ../scripts/backtest_independent_projections.py evaluate            # frozen selection -> held-out
uv run python ../scripts/backtest_independent_projections.py fit                 # data/models/independent_projections_ifp-1.0.0.json
uv run python ../scripts/backtest_independent_projections.py project --week 7    # writes the two live tables
```

`select`/`evaluate`/`fit`/`audit` open the live DB read-only. Only `project` writes, and only the
tables `independent_projection_runs` and `independent_player_projections` (create-only, no schema
version bump). `evaluate` refuses a selection made on other seasons or other scoring; `fit` refuses a
held-out report that does not hash-match the current `selection.json`. Override paths with
`--live-db`, `--history-db`, `--out` or `NFL_HISTORY_DB_PATH` / `INDEPENDENT_MODEL_DIR`.

## Data audit (real local data, 2026-10-14)

Live DB (`data/fantasy.duckdb`): **2026 only**, NFL weeks 1-5 complete (79 final games), 2,022
QB/RB/WR/TE player-games. ESPN projection history has **one** snapshot date (2026-10-14) covering
weeks 6-17; 525 week-6 player-weeks are pregame, **0** have a completed actual. ESPN weekly
`projected_points` for weeks 1-5 were captured after kickoff and are never used. ID map: 1,017
high-confidence ESPN->GSIS links, 3 unmatched, 32 D/ST.

History DB (via `sync_nfl_history.py --seasons 2019-2025`):

| Season | Player-games | QB | RB | WR | TE | xFP cov. | Snap cov. | PBP cov. |
|---|---|---|---|---|---|---|---|---|
| 2019 | 6,220 | 598 | 1,713 | 2,489 | 1,420 | 80% | 99.9% | 99.6% |
| 2020 | 6,505 | 631 | 1,784 | 2,559 | 1,531 | 79% | 99.95% | 100% |
| 2021 | 6,883 | 670 | 1,801 | 2,772 | 1,640 | 78% | 99.9% | 100% |
| 2022 | 6,807 | 647 | 1,840 | 2,679 | 1,641 | 79% | 99.99% | 100% |
| 2023 | 6,834 | 680 | 1,737 | 2,803 | 1,614 | 78% | 99.9% | 100% |
| 2024 | 6,816 | 682 | 1,737 | 2,735 | 1,662 | 78% | 99.9% | 100% |
| 2025 | 6,878 | 674 | 1,749 | 2,776 | 1,679 | 78% | 99.8% | 100% |
| 2026 (live) | 2,022 | 186 | 517 | 804 | 515 | 77% | 99.75% | 100% |

xFP is missing for ~22% of appearances, mostly snap-only appearances with no opportunity; it is
imputed with an indicator, never zero-filled. Not available and not modeled: route participation,
coverage splits, pressure-adjusted QB metrics, injury probabilities, depth-chart intent, betting
lines (nflverse stores closing lines, which post-date a midweek cutoff).

## League scoring

`resolve_skill_scoring` maps ESPN stat ids to nflverse columns: 3 passing yards, 4 passing TD,
19/26/44 two-point conversions, 20 interceptions, 24/42 rushing/receiving yards, 25/43 TDs,
53 receptions, 63 fumble-recovery TD, 72 fumbles lost (`fumbles_lost_total`: ESPN counts
special-teams fumbles), 101+102 return TDs (`special_teams_tds`, only when equal).
Kicker statistics and D/ST-only overrides (slot 16) never score offense. Defensive-return events
with non-zero base points (93, 103, 104, 206, 209) are listed as **unmodeled**.

Refused (model unavailable, `coverage_status: unsupported_scoring`): yardage/TD-length bonuses,
first downs, per-target or per-carry points, any slot-specific override other than D/ST (e.g.
TE premium), reverse items, unequal kick/punt return TDs. Half-PPR or standard work because the
weights change, and the **scoring fingerprint** changes: an artifact trained under one
fingerprint refuses leagues with another.

**Verification:** for 2026 weeks 1-5, computed league points match ESPN's stored actuals on
**1,700 of 1,701** high-confidence player-weeks (99.94%; QB/RB/TE 100%, one WR off by 2.0).

## Point-in-time features

A forecast for (season S, week W) uses only games with (season, week) < (S, W), from S and S-1.
`features_for_week` applies this filter itself. Opponent and team context use only that
season's games before W. The target week contributes only team, opponent and home/away.

| Feature | Meaning | Source |
|---|---|---|
| `pts_recent` | League pts/game, last <=5 games, shrunk 2 games toward the position mean | box score + scoring |
| `pts_l3`, `pts_season`, `pts_prev_season` | Last-3 mean; season mean (shrunk); previous season | box score |
| `pts_std` | Volatility over last <=5 games | derived |
| `xfp_recent` | nflverse xFP/game (nflverse PPR units), shrunk | `nfl_ff_opportunity` |
| `pts_minus_xfp` | Efficiency vs xFP (unit mix noted; elite players can sustain it) | derived |
| `targets_pg`, `carries_pg`, `pass_att_pg`, `air_yards_pg` | Per game, last <=5 | box score |
| `target_share`, `rush_share` | Player / team totals over the window | box score + `nfl_team_week` |
| `rz_opps_pg`, `gl_carries_pg` | Red-zone targets+rushes, inside-5 rushes | `nfl_pbp_player_week` |
| `snap_pct`, `snap_trend` | Mean snap share; last game minus earlier games | `nfl_snap_counts` |
| `opp_trend` | Last game's targets+carries minus earlier mean | box score |
| `games_recent`, `games_season`, `weeks_since_last`, `team_changed` | Sample and role-change context | derived |
| `team_pts_pg` | Team points/game vs league (shrunk 3 games) | `nfl_schedules` |
| `opp_pos_allowed` | Opponent league pts allowed to the position vs league (shrunk 4 games; 0 in week 1) | panel |
| `opp_pass_epa`, `opp_rush_epa` | Opponent EPA/play allowed vs league (shrunk 150 plays) | `nfl_defense_week` |
| `is_home` | Home game | `nfl_schedules` |
| `xfp_missing`, `snap_missing`, `rz_missing` | No recent coverage | derived |

A snap-only appearance is a measured zero for box-score stats; a missing team play-by-play game
leaves red-zone fields null. Features are rounded to 1e-10 so runs are bit-reproducible.

## Models

* **A1 recent_avg**: raw league pts/game over the last <=5 games.
* **A2 season_avg**: raw season pts/game, previous season if no current games.
* **B opportunity**: per-position ridge on opportunity/role features only.
* **C context**: B plus production, efficiency, role change, team offense, opponent, home, flags.

Ridge on standardized features, unpenalized intercept, penalty = lambda * n, missing values imputed
with the **training** mean plus a `__missing` indicator, predictions floored at 0. Chosen over
boosted trees because the per-position samples (4k-17k rows) are modest, coefficients give exact
additive explanations, and nothing larger was justified. Players with no game in the lookback
window abstain (no position-average guess).

## Validation protocol

* Feature warm-up 2019; training from 2020; **validation 2023-2024** (lambda grid, B vs C,
  interval bands); **held-out 2025 + 2026 weeks 1-5**.
* Walk-forward: every test week refits on strictly earlier weeks (expanding window).
* Population: player-weeks where the player appeared. Estimates are **conditional on playing**.
* `PROMOTION_CRITERIA` (in `forecast_backtest.py`, committed before the held-out run):
  >= 400 held-out observations; >= 3% MAE improvement over the better trailing baseline; week-
  clustered bootstrap 95% CI of the MAE difference entirely below 0 (1,000 resamples, seed
  20261014); 80% intervals published only if held-out coverage is 75-85%; Start/Sit readiness
  additionally needs >= 300 matched pregame ESPN observations per position with primary MAE <= ESPN.

## Results (REAL data, held-out, run once with the frozen selection)

Selection (validation 2023-24): context model C primary everywhere (validation MAE
QB 6.06 / RB 4.02 / WR 4.24 / TE 2.86 vs B 6.29 / 4.16 / 4.36 / 2.95).

Held-out, same player-weeks for every model (common support):

| Pos | n | Recent avg | Season avg | Opportunity (B) | Context (C) | Bias C | RMSE C | 95% CI C - best baseline |
|---|---|---|---|---|---|---|---|---|
| QB | 834 | 6.95 | 6.97 | 6.67 | **6.55** | -0.07 | 8.14 | [-0.57, -0.23] |
| RB | 2,196 | 4.21 | **4.07** | 4.20 | 4.10 | -0.03 | 5.89 | [-0.09, +0.12] |
| WR | 3,477 | 4.20 | 4.29 | 4.20 | **4.08** | -0.17 | 5.74 | [-0.19, -0.05] |
| TE | 2,138 | 3.20 | 3.13 | 3.14 | **3.05** | +0.03 | 4.58 | [-0.18, -0.01] |

Bias = mean(actual - projected). Coverage 96.9-97.5% (the rest abstain: 26 QB, 70 RB, 103 WR, 56 TE
debut/long-absence appearances). By season, C beat both baselines for WR/TE in 2025 and 2026; RB
lost to the 2025 season average. Cohorts: C is better for projections >= 10 at every position, but
worse than baselines for low-volume RBs (< 5 opportunities/game) and low-sample RB/WR/TE (< 3 games).

80% interval coverage on held-out: QB 74.9% (withheld: outside 75-85%), RB 79.3%, WR 80.1%,
TE 80.0%. Intervals are prediction intervals for one week's score, not confidence intervals for a mean.

**Start/Sit pairs** (same position-week, pregame pool QB 24 / RB 48 / WR 60 / TE 24 by recent avg;
78,995 pairs, 355 actual ties skipped): pick-accuracy recent 62.5%, season 63.0%, opportunity 64.0%,
**context 65.2%**; mean regret 2.82 / 2.82 / 2.64 / **2.51** pts. Close calls (recent averages within
3 pts, 31,155 pairs): 54.4% / 56.7% / 58.2% / **60.1%**.

**Real league rosters** (2026 weeks 1-5, same-position players on the same fantasy roster,
contemporaneous lineup snapshots, 1,012 pairs): recent 64.6%, season 62.3%, opportunity 65.0%,
context 64.7%. No meaningful edge on this small sample.

**ESPN comparison: 0 matched observations.** Nothing is claimed about ESPN.

Runtime (Apple silicon laptop): history ingestion 17 s; feature store 42.7k rows in 3.3 s;
selection 4.4 s; held-out evaluation 5.4 s; final fit 3.4 s; live week run 0.45 s for 657
players; API read ~0.12 s. Artifact: 29 KB JSON (coefficients only, no player data).

## Live runs and abstention

Week-W rows (`coverage_status`): `projected`; `abstain_no_history`; `bye`;
`withheld_unavailable` (NFL-ineligible for W per `player_availability`, ESPN OUT / INJURY_RESERVE /
SUSPENSION, or latest weekly roster status not ACT/INA, e.g. exempt list); `uncertain_id_mapping`
(anything but a high-confidence, conflict-free ESPN->GSIS link); `unsupported_position` (K, D/ST);
`unsupported_scoring`; `no_model`. Fantasy IR and ESPN QUESTIONABLE/DOUBTFUL are flags only.

## API contract (`independent-projections/v1`)

`GET /api/projections/independent?week=7[&position=WR][&team_id=3]` (default week: league
`current_week`). 200 with `available: false` when no run exists; 422 for a bad position.

```json
{
  "schema_version": "independent-projections/v1",
  "league": {"id": 123, "season": 2026}, "week": 7,
  "available": true, "unavailable_reason": null, "notes": ["..."],
  "run": {"run_id": "ifp-2026w7-...", "model_version": "ifp-1.0.0", "artifact_created_at": "...",
          "created_at": "...", "scoring_fingerprint": "e165b59686110c7d",
          "feature_cutoff": {"data_through_season": 2026, "data_through_week": 6,
                             "rule": "only NFL games completed before the target week"},
          "nfl_synced_at": "...", "heldout_evaluation": {"...": "aggregates"}},
  "model_status": {"WR": {"status": "experimental", "validated_for": null, "start_sit_ready": false,
                          "intervals_published": true, "reasons": ["relative MAE improvement 0.0276 < 0.03", "..."]}},
  "players": [{
    "espn_player_id": 1001, "gsis_id": "00-0000001", "name": "Example WR", "position": "WR",
    "nfl_team": "AAA", "opponent": "BBB", "is_home": true, "fantasy_team_id": 3, "injury_status": "ACTIVE",
    "independent": {
      "projected_points": 11.4, "interval_80": {"low": 3.1, "high": 22.7, "nominal_coverage": 0.8},
      "model": "context", "model_version": "ifp-1.0.0", "model_status": "experimental",
      "start_sit_ready": false, "coverage_status": "projected", "games_used": 5,
      "latest_usage": {"season": 2026, "week": 6}, "missing_inputs": [], "flags": [],
      "explanation": [{"feature": "target_share", "value": 0.27, "training_mean": 0.119,
                       "contribution": 1.9, "text": "target share: 0.270 (training average 0.119)"}],
      "is_espn_fallback": false},
    "espn": {"projected_points": 9.8, "synced_at": "2026-10-20 12:00:00"},
    "comparison": {"independent_minus_espn": 1.6, "comparable": true, "comparability_note": null,
                   "evidence": [{"kind": "model_input", "feature": "target_share", "contribution": 1.9,
                                 "text": "target share: 0.270 (training average 0.119)"}]}
  }]
}
```

Rules for consumers: `null` means unavailable, never zero. `independent` is never filled from
ESPN (`is_espn_fallback` is always false). Use `independent_minus_espn` only when
`comparable` is true: ESPN projects 0 for players it does not expect to play, while the
independent estimate assumes the player plays. `evidence` lists the model's own largest input
contributions (points relative to the training-average player) plus data-quality flags; it never
explains ESPN's number. Players are ordered by independent projection (nulls last), then id.

## Known biases and unsupported populations

* **Conditional on playing.** No participation model: backups, game-time decisions and
  players returning from absence get an estimate as if they play. 73 of 386 live week-6
  estimates had an ESPN projection of 0 (marked not comparable).
* **`snap_trend` conflates an in-game injury exit with a demotion.** The largest comparable
  week-6 disagreements (Barkley, Rice, Moore) are driven by a last-game snap drop.
* Low-volume RBs and players with < 3 recent games are worse than simple averages.
* Team changes carry old-team shares (flagged); rookies abstain until their first game.
* No injury reports, depth charts, coaching or QB changes as inputs; no weather or Vegas.
* Label noise: one 2026 WR player-week differs from ESPN by 2.0 (unexplained).
* K and D/ST are not modeled.

## Recommended next steps

1. Keep syncing ESPN daily so `espn_projection_history` accumulates true pregame snapshots;
   rerun `evaluate` once >= 300 matched completed player-weeks per position exist.
2. Add a participation model (P(plays) from roster status, injury report and snap history) so
   expected points are unconditional, and evaluate it on the roster-based population.
3. Separate in-game injury exits from role changes (e.g. game-level snap counts by quarter or
   injury report the following week) before trusting `snap_trend`.
4. Only then let the Start/Sit page show both lineups, with the model status visible.

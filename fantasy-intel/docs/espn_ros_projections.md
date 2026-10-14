# ESPN forward projections (V2 baseline)

ESPN's weekly projections are the app's **free forward fantasy-point baseline**. They are
ESPN's numbers, stored and shown unmodified, with explicit provenance (`projection_source = 'ESPN'`).
They are not our model. Later V2 layers (role quality, sustainability, offensive environment,
schedule, scarcity, lineup impact, manager behavior) are evaluated separately and do not alter them.

They **do not** feed `opportunity_score`, `production_score`, `value_score`, market signals
(BUY/HOLD/SELL), `production_sustainability`, `waiver_score`, manager interest, suggested drops
or `team_position_needs`. Tests assert this, and a counterfactual on the real league
(future projections removed, or all set to 0) left every analytics table identical.

## Fetch

One request per remaining fantasy week, for `w = current_week .. final_scoring_period` (inclusive,
from league settings; never hardcoded, never NFL week 18 when the league ends at 17):

```
GET .../leagues/{id}?view=kona_player_info&scoringPeriodId=w
x-fantasy-filter: {"players": {"filterStatus": {"value": ["ONTEAM", "FREEAGENT", "WAIVERS"]},
                               "limit": 3000, "sortPercOwned": {"sortPriority": 1, "sortAsc": false}}}
```

`filterStatus` is ESPN's own player-status filter. The combined request returns exactly the
union of the separate ONTEAM and FREEAGENT+WAIVERS requests (verified on the real league:
190 + 862 = 1,052 players), so there is no name or ID lookup. Only `stats[]` entries with
`statSourceId 1`, `statSplitTypeId 1`, `scoringPeriodId w` are read from week `w`'s response.
Season-level projections (split 0, which include NFL week 18) are not used: the ROS total is
built from the weekly rows instead.

Los Subies (week 6, final period 17): 12 requests per sync.

## Storage

| table | contents |
|---|---|
| `espn_player_weekly_points.projected_points` | ESPN projection per player-week (reused, not a new table). `actual_points` is never written by the projection fetch. |
| `espn_projection_history` | one snapshot per `(snapshot_date, season, player, week)`; the latest sync of the day wins. Calibration/backtesting only. |
| `player_ros_projections` | derived ROS summary, one row per player per season. |

`0` = ESPN projected zero (a measured value). No row or NULL = no ESPN projection (unknown). A
missing value is never converted to 0.

## Replacement semantics (stale data)

Each requested week is handled independently:

| outcome for week N | `projected_points` for week N | today's history for week N |
|---|---|---|
| **complete**: request succeeded, response not truncated (`< limit` players), and it contains at least 95% of the currently rostered players | cleared first, then rewritten from this sync (roster + FA pool + the projection response). A player ESPN no longer returns, or returns without a projection, ends with no projection. Rows left with neither an actual nor a projection are deleted. | replaced |
| **partial**: succeeded but truncated or missing rostered players | observed projections merged in; older values for players not returned are kept; sync warning | observed rows upserted |
| **failed**: network/HTTP error | untouched: previous values kept (stale is safer than turning an API failure into "missing"); sync warning | untouched |

One failed week never affects the other weeks. Past weeks (`< current_week`) are never in the
fetch window, so their stored projections and actuals stay as they were. Current-week values from
rosters and the FA pool are written in the same pass, so they merge with the projection response
(and a pool-only player keeps its pool projection).

## Coverage

Per week, the sync stats (`sync_runs.stats.projections.coverage`, shown on `/data`) record:

- `universe`: current rostered players ∪ every player ESPN returned (expected), returned,
  projected (non-null), zero, missing (expected − projected)
- the same split for `rostered` and `available` (free agents/waivers returned)
- `rows_returned`, `truncated`, `complete`, plus `weeks_replaced` / `weeks_partial` / `weeks_failed`
  and each failure message

`/data` also shows what is stored *now* per week, which differs from the latest fetch only when a
week failed.

## ROS summary (`player_ros_projections`)

Rows: every rostered player plus every player with an ESPN projection in at least one remaining week.

- **Expected weeks**: every fantasy week `current_week .. final_scoring_period` (bye weeks included:
  ESPN projects 0 for a bye, which counts as available).
- **Available**: weeks with a non-null ESPN projection (`0` counts).
- **Completeness rule**: a total is set only when *every* expected week in its window is available
  (`ros_coverage = 1`). Otherwise it is NULL, with `*_weeks_expected`, `*_weeks_available`,
  `ros_coverage` and `missing_weeks` exposed. A partial sum would look like an authoritative low projection.
- **Regular season / playoffs**: `last_regular_season_week` = the last scoring period of matchup
  period `reg_season_matchups`, read from `settings.scheduleSettings.matchupPeriods` (multi-week
  matchups are handled). Without that map it equals `reg_season_matchups` only when
  `matchupPeriodLength` is 1; otherwise the split is unknown and both split totals are NULL.
  Regular = `current_week .. last_regular_season_week`; playoffs = `last_regular_season_week + 1 .. final_scoring_period`.
  Los Subies: 14 matchups, final 17, so regular = weeks 6–14 and playoffs = 15–17.
  If the regular season is over, its window is empty and its total is 0 (nothing remains).
- **Byes**: from `nfl_schedules` (REG games). The player's current NFL team has no game in a week the
  schedule covers. Never inferred from a 0 projection. `bye_weeks_remaining` is NULL when the team or
  the schedule is unknown for any remaining week. A 0 projection in a non-bye week (injury, role, ESPN's
  call) stays 0.

The table is rebuilt by the ESPN sync and by the analytics run (which picks up schedule refreshes).

## API

- `GET /api/players/{id}/forward` and `forward_projection` on `GET /api/players/{id}`:
  `{source: "ESPN", note, summary, weeks: [{week, phase, opponent, is_home, on_bye,
  espn_projected_points, projection_available}]}` for every remaining fantasy week.
  Opponent comes from the NFL schedule. No matchup or schedule adjustment is applied.
- `GET /api/data-quality` → `espn_projections`: latest fetch stats, stored per-week counts,
  ROS coverage summary, incomplete players, history size.

## Schema v5

Create-only migration: adds `espn_projection_history` and `player_ros_projections`. No table is
dropped. ESPN/nflverse raw tables, `espn_player_weekly_points` and the external utilization tables
and history are preserved. The read-only API asks you to run a sync until the DB is migrated.

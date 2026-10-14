# ESPN projection availability: empirical probe (2026, week 6)

Read-only probe against the real synced league (Los Subies, current scoring period 6,
final fantasy period 17), run 2026-10-14. No payloads or player values are stored here.

## Stat entry keys

ESPN `player.stats[]` entries are keyed by `seasonId`, `scoringPeriodId`,
`statSourceId` (0 = actual, 1 = ESPN projection) and `statSplitTypeId`:

| source / split | period | meaning (observed) |
|---|---|---|
| 0 / 1 | w | actual league-scoring points for week w |
| 0 / 0 | 0 | season-to-date actual total |
| 1 / 1 | w | ESPN projection for week w |
| 1 / 0 | 0 | **rest-of-season projection = Σ weekly projections, current week through NFL week 18** (median abs diff 0.000 over 189 rostered players; it includes week 18 even though the fantasy season ends at 17) |
| 1 / 2 | 0 | another season-level projection; semantics **unverified** (not equal to actual + ROS). Not stored |

## Which request returns what

| request | weekly projections returned |
|---|---|
| `kona_playercard` (our player-card sync), `max_scoring_period` 17 **or** 18 | **none** (only weekly actuals, season actual and 1/0 ROS); identical for 17 and 18, so `max_scoring_period` is not the limiter |
| `kona_playercard` + explicit `additionalValue` `112026{w}` for w = 1..18 | only the current week (6) |
| `kona_player_info` with `scoringPeriodId = w` (filterStatus + limit + sort; `filterIds` alone returns HTTP 400) | **week w**, for any w = 6..18 |
| `mRoster` with `scoringPeriodId = w` | weeks w-1 and w |

## Coverage of `kona_player_info?scoringPeriodId=w` (w = 6..18)

Every week 6-18: 189/190 rostered players and 149/150 of the top-owned free agents
carry a 1/1 projection for week w. Zero projections per week (rostered): 40, 40, 34, 17,
30, 39, 8, 30, 19, 8, 8, 8, 8. The spikes line up with NFL bye weeks.

- **Bye weeks**: projection is 0 for the bye week (e.g. a BUF QB projects 0 in week 7, BUF's bye).
- **Injuries**: an IR player projected 0 for the current week but normal values for later weeks
  (ESPN assumes a return); IR status alone doesn't zero future weeks.

## Ingestion status

- Fixed (this branch): the free-agent pool request already returned each FA's current-week
  projection, but it wasn't stored (only rostered players' were). It's now stored: 419
  previously dropped FA projections in the week-6 payload.
- Ingested since (V2 baseline): one `kona_player_info` request per remaining fantasy week
  (current..final fantasy period, never week 18 for a league ending at 17), ONTEAM + FREEAGENT +
  WAIVERS in a single request, stored in `espn_player_weekly_points.projected_points`. The ROS
  total is derived from those weekly rows, not from the 1/0 season split. See
  [`espn_ros_projections.md`](espn_ros_projections.md).
- These are **ESPN projections**, not our model. They don't affect scores, waivers or trades.

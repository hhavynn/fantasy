# Sleeper source: phase 1 foundation

Read-only Sleeper support at `backend/app/sleeper/`. It is a library only in this phase: nothing
writes Sleeper data to DuckDB, and no API route or page uses it yet. The ESPN path is unchanged.

## Modules

| module | role |
|---|---|
| `client.py` | HTTP: `league`, `league_users`, `league_rosters`, `nfl_state`, `players`. In-memory TTL cache (60s) for league calls; `/players/nfl` stored at `data/sleeper/players_nfl.json` and refetched only when older than 24h (Sleeper's limit), with the stale copy used if a refresh fails. 429/5xx/network errors retried (Retry-After, else 1s, 2s, 4s; capped at 30s). |
| `models.py` | Raw JSON -> frozen dataclasses. `sid()` keeps every id a string. |
| `normalize.py` | Models -> the row shapes `espn/parse.py` emits (`leagues`, `league_members`, `fantasy_teams`, roster/lineup, player rows), same column names where meaning matches, plus `provider = "sleeper"`. |
| `league.py` | `fetch_league(client, league_id)`: the only loader, explicit league id required. |
| `id_map.py` | Sleeper id -> gsis_id via `nflreadpy.load_ff_playerids()` (cached 24h at `data/sleeper/ff_playerids.json`); Sleeper's own `gsis_id` as a medium-confidence fallback; team defenses map to an NFL team; conflicts and misses are reported, never dropped. |

## Identifier rules

* League, user, owner, co-owner, player, draft and bracket ids are strings everywhere. A league id
  passed as an int raises `TypeError` instead of being converted (Sleeper league ids exceed 2^53,
  so a float or JavaScript number would already point at the wrong league).
* Sleeper itself sends `bracket_id` / `loser_bracket_id` as JSON numbers above 2^53; they are
  stringified exactly on parse. Any JS client must never see them as numbers.
* `nflreadpy.load_ff_playerids()` infers `sleeper_id` as Int64; it is cast to a string column
  before use. Sleeper player ids have no leading zeros (checked against all 12,229 players), so
  this round trip is exact.
* `roster_id` stays an int: it is a 1..N slot number (Sleeper sends an int), the analogue of
  ESPN `team_id`, not an entity id.
* Team codes are normalised to nflverse (`LAR` -> `LA`, `OAK` -> `LV`; the ID map's `KCC`,
  `LVR`, `GBP`, ... likewise).

## Slot / position mapping

`QB RB WR TE K` as is, `DEF` -> `D/ST`, `FLEX` -> `FLEX`, `SUPER_FLEX` -> `OP`,
`WRRB_FLEX` -> `RB/WR`, `REC_FLEX` -> `WR/TE`, `BN` -> `BE`, `reserve` -> `IR`, taxi -> `TAXI`
(rostered, never starts). `roster_slots` from a Sleeper league feeds `analytics.lineup.lineup_slots`
unchanged (tested).

## Not yet league-scoped: global "latest league" lookups (phase 2)

Every new code path takes an explicit league id. These existing ones pick the most recently
synced league instead (`... FROM leagues ORDER BY synced_at DESC LIMIT 1`, or the same within a
season):

| location | used by |
|---|---|
| `api/routes.py` `_league()` | `/overview`, `/players`, `/players/{id}`, `/players/{id}/forward`, `/waivers`, `/managers`, `/managers/{team_id}`, `/lineup-projections`, `/teams/{team_id}/lineup-projection`, `/trades/options`, `/trades/evaluate` |
| `api/routes.py` `status()` | `/status` |
| `api/routes.py` `_projection_quality()` | `/data-quality` (also takes the latest ESPN `sync_runs` row, not per league) |
| `api/trade_discovery.py` `discover()` | `/trades/discover` |
| `analytics/trade_data.py` `load_league_state()` | trade evaluator, trade discovery, `playoff_race.load_race_data`, `team_value.load_team_value_inputs` |
| `analytics/luck_lab.py` `load_luck_data()` | `/luck-lab` |
| `analytics/manager_loader.py` `load_league()` | `load_inputs` (manager metrics), `manager_scouting` |
| `analytics/waivers.py` `build_waivers()` | analytics pipeline |
| `analytics/projections.py` `league_window(season)` | ROS projections, team lineups, `/data-quality` |
| `analytics/schedule_intelligence.py` `league_info()` | schedule intelligence |
| `analytics/pipeline.py` `_next_week()`; `has_espn = count(*) FROM leagues` | analytics pipeline |
| `scripts/check_manager_scouting.py` | local audit script |
| `config.Settings.league_id` (int, from `ESPN_LEAGUE_ID`) -> `espn/sync.py` | the one league the ESPN sync knows about |

Structural single-league assumptions in the same family:

* `fantasy_players` has no `league_id`, yet stores league-specific state (`availability_status`,
  `on_team_id`); `player_id_map` is keyed by `espn_player_id` only.
* `leagues.league_id` and every `league_id` column are `BIGINT`; `fantasy_players.espn_player_id`
  is the primary key.
* The in-memory models (`team_lineups.RosterPlayer.espn_player_id: int`, `Projections`,
  `trades.LeagueState.rosters: dict[int, ...]`, `lineup.py` tie-breaks on `espn_player_id`) are keyed
  by ESPN integer ids, which is why Sleeper data is normalised to rows here rather than to
  `LeagueState`.
* `fantasy_teams.is_mine` assumes one viewer per database.

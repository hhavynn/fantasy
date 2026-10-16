# Proposal: league-keyed analytics schema (Phase 2, not applied)

Status: proposal. Phase 1 adds **no** change to `backend/app/db/schema.sql` and bumps no
`SCHEMA_VERSION`. Normalized league data for new providers lives in the separate tenancy store
(`league_snapshots`). This document is the plan for bringing the analytics tables under the same
league context so the existing engines can serve more than one league.

## Principles
1. **One league key everywhere**: `league_key VARCHAR` = the internal opaque league id from the
   tenancy registry (`lg_...`). Provider-natural ids stay as provenance columns
   (`provider`, `external_league_id VARCHAR`).
2. **Namespaced player ids**: `player_key VARCHAR` = `provider:raw_id`; `gsis_id` stays the bridge
   to nflverse and is filled only from id-based evidence (`identity.IdentityLink`).
3. **League-scoring values are league-keyed**; league-independent facts (nflverse stats, NFL
   rosters, schedules) stay global.
4. **Personalization leaves league data**: `is_mine` moves to `league_connections.my_team_external_id`.
5. Additive first, switch readers, then drop: never a destructive rebuild of stored history
   (`independent_projection_*`, `projection_performance_reports` are append-only by contract).

## Table-by-table

| Table | Today | Proposed |
|---|---|---|
| `leagues` | PK (league_id BIGINT, season) | add `league_key`, `provider`, `external_league_id VARCHAR`; PK → `league_key` |
| `league_members`, `fantasy_teams`, `matchups`, `draft_picks`, `lineup_snapshots`, `team_*_projections`, `manager_*`, `team_position_needs`, `waiver_recommendations` | league_id BIGINT, team_id INTEGER | add `league_key`; `team_id` → `team_key VARCHAR` (external id as string); drop `fantasy_teams.is_mine` |
| `fantasy_players` | PK espn_player_id; holds league availability | split: `provider_players` (player_key PK, name, position, pro_team, provider-level fields) and `league_player_availability` (league_key, player_key, status, on_team_key, ...) |
| `espn_player_weekly_points`, `espn_projection_history`, `player_ros_projections` | season + espn id; league scoring | rename to `league_player_points` / `league_projection_history` / `league_ros_projections` keyed by `league_key, player_key, ...` |
| `independent_projection_runs/rows`, `independent_player_projections` | season-wide; scoring from latest league | add `scoring_hash` (sha256 of canonical scoring rules) to runs; rows keyed by run; consumers pick runs by league scoring hash, so two leagues with identical scoring share runs safely |
| `transactions`, `transaction_items`, `activity_messages` | id-only PKs | PK (`league_key`, `transaction_id`); add `league_key` to `transaction_items` |
| `player_id_map` | ESPN → GSIS | generalize to `player_identity` (player_key PK, gsis_id, method, confidence, reason) populated from `IdentityLink`s; ESPN `name_position_team` rows become `unmatched` for cross-provider use |
| `nfl_*`, `player_week_usage`, `player_context`, `defense_context`, `team_context`, `team_game_lines` | global | unchanged (league-independent) |

## Read path
- Replace every `ORDER BY synced_at DESC LIMIT 1` selector (21 sites, pinned by
  `tests/test_league_scope_audit.py`) with `load_league_state(con, ctx: LeagueContext)`; the
  owner deployment resolves its context from `ESPN_LEAGUE_ID`/`ESPN_SEASON` via a legacy shim, so
  its behaviour does not change.
- Every engine query gains `WHERE league_key = ?`; tests seed two leagues with conflicting data and
  assert each engine's output is unchanged by the other league.

## Hosted path
- The shared MotherDuck database stays owner-only. Multi-tenant serving needs either per-tenant
  databases/shares or a Postgres data plane with row-level security on `league_key`; the choice is
  a Phase 3 ADR. Until then `publish.py` must never run against a database containing other users'
  leagues.

## Rollout
1. Additive columns + backfill for the owner league (`league_key` from the registry's legacy row).
2. Engines switch to `LeagueContext` one at a time behind the ratchet test.
3. Drop legacy columns after a full season of dual-write.

# ADR-0001: Explicit league context, tenancy and provider adapters

- Status: Accepted for Phase 1 (foundation only; nothing user-facing is enabled by default)
- Date: 2026-10-16
- Scope: `fantasy-intel/backend/app/leagues/**`, `/v2` API, `/leagues` prototype page
- Related: [provider capability matrix](provider-capability-matrix.md), [threat model](threat-model.md),
  [schema migration proposal](schema-migration-proposal.md), [roadmap](roadmap.md)

## Context

Fantasy Intelligence is a single-owner ESPN dashboard. One ESPN league is configured through
environment variables (`ESPN_LEAGUE_ID`, `ESPN_SEASON`, `ESPN_MY_TEAM_ID`, `ESPN_S2`, `SWID`),
synced into one DuckDB file, optionally published wholesale to one MotherDuck database, and
served by a FastAPI app whose only access control is a shared `FANTASY_API_KEY` header (plus an
optional `SITE_PASSWORD` basic-auth gate in Next.js). That is correct for one owner and unsafe
for anyone else.

The long-term goal is that any user can connect ESPN, Sleeper or Yahoo leagues, pick a team, and
get league-specific analysis, without ever seeing another user's private league, credentials,
settings or cached results.

## Audit: single-league assumptions in today's code

### Configuration and identity
| Where | Assumption |
|---|---|
| `app/config.py` `Settings.league_id/season/my_team_id/espn_s2/swid` | Exactly one league, one owner, one set of ESPN cookies per deployment. |
| `app/main.py` `require_api_key` | `FANTASY_API_KEY` authenticates *the dashboard server*, not a person. Everyone past it is the owner. |
| `frontend/proxy.ts`, `lib/site-auth.ts` | One shared `SITE_PASSWORD`; any username accepted. No user identity reaches the backend. |
| `frontend/lib/api.ts` | Every call is unscoped (`/league`, `/waivers`, ...); no league or user in the request. |

### "Most recently synced league, globally" selectors (21 sites)
Every one of these silently picks `ORDER BY synced_at DESC LIMIT 1` across *all* leagues in the
database. With two leagues in one database they return whichever synced last, for every caller.
`tests/test_league_scope_audit.py` pins this list so a new global selector fails CI.

- `app/analytics/trade_data.py` `load_league_state` (Trade Analyzer, Trade Discovery, Team Value)
- `app/analytics/waivers.py` (waiver engine), `app/analytics/manager_loader.py` (managers/scouting)
- `app/analytics/matchup.py`, `luck_lab.py`, `weekly_recap.py`, `start_sit.py`, `schedule_intelligence.py`
- `app/analytics/projections.py`, `projection_source.py`, `independent_projection_store.py` (scoring rules!),
  `promotion_tracker.py`, `pipeline.py`, `research.py` (x2)
- `app/api/routes.py` (x3: `/league`, sync status, data quality), `api/independent_projections.py`, `api/trade_discovery.py`

### Storage without a league key, holding league-specific values
| Table | Problem in a multi-league database |
|---|---|
| `fantasy_players` (PK `espn_player_id`) | `availability_status`, `on_team_id`, `percent_owned` are *one league's* view. A second league overwrites the first. |
| `espn_player_weekly_points`, `espn_projection_history`, `player_ros_projections` | ESPN `appliedTotal` / projections are in *that league's* scoring; keyed only by season + player. |
| `independent_player_projections`, `independent_projection_runs` | Points derived with the scoring of "the latest league of the season". |
| `fantasy_teams.is_mine` | Per-user personalization stored in shared league data. |
| `transactions`, `activity_messages` (PK id only), `transaction_items` (no `league_id`) | Ids are only unique within ESPN; nothing namespaces the provider. |
| `league_id BIGINT`, `team_id INTEGER`, `espn_player_id BIGINT` everywhere | ESPN-shaped ids. Sleeper league ids exceed 2^53 and Yahoo keys are strings (`449.l.12345`). |

### Caches
- `app/api/trade_discovery.py` `_CACHE` keys on `(data_version, anchor team, ...)`: no league, no tenant.
  Correct only while one database holds one league for one owner.
- `app/api/deps.py` keeps one MotherDuck connection per process for one database name.

### Hosted data path
- `app/db/publish.py` copies **every table** of the local file to one MotherDuck database. Any
  tenant-private data written into the analytics DB would be published with it, and the hosted API
  reads that database with one service token. It is not a tenancy boundary.

### Identity
- `app/nfl/mapping.py` falls back to a unique normalised-name + position match
  (`name_position_team`, medium confidence). Acceptable for the owner's dashboard where it is
  labelled; not acceptable as cross-provider identity evidence.
- `app/sleeper/id_map.py` (already merged) is id-evidence only. Good; reused.

## Decision

### 1. An explicit, authorized `LeagueContext` is the only way to touch league data (new code)
`app/leagues/context.py`

- `LeagueKey(provider, external_league_id: str, season)` is the provider-natural identity.
  External ids are always strings and validated per provider; `espn:12345:2026` and
  `sleeper:12345:2026` are different leagues.
- The internal league id is opaque random (`lg_` + 144 bits), never derived from the external id.
- `Principal(user_id)` comes from a verified user assertion, never from a query parameter.
- `LeagueContext` carries principal, role, access method, chosen team, freshness and provider
  capabilities. It can only be built by `issue_context`, which the tenancy store calls after it
  has verified an active connection; constructing one by hand raises. Store reads re-check the
  connection, so a context obtained before revocation stops working.
- Unauthorized and unknown leagues are indistinguishable (`404`), so ids cannot be probed.

### 2. Tenancy lives in a separate control-plane store, never in the published analytics DB
`app/leagues/store.py`, `tenancy_schema.sql`, `FANTASY_TENANCY_DB_PATH`

- Tables: `app_users`, `league_registry` (unique on provider + external id + season),
  `league_connections` (per user, revocable; *this* is the authorization grant),
  `league_sync_status`, `league_snapshots` (normalized settings/teams/rosters + provenance),
  `credential_refs` (metadata only).
- One registry row per provider league; access is per user via connections. A private league
  can only be connected with a credentialed method that user holds (owner env today, OAuth
  later); `PUBLIC` access is accepted only when the provider itself serves the league without
  credentials.
- Phase 1 uses DuckDB for parity with the codebase and tests. Production multi-tenancy needs a
  transactional multi-writer database with row-level security (Postgres; see roadmap). The store
  interface is written so that swap does not touch callers.
- `publish.py` is unchanged and only ever copies the analytics file; the tenancy file is not in it.

### 3. Tenant-scoped cache keys
`app/leagues/cache.py`: every key embeds a hash of the tenant id, the provider, the internal
league id, the season and the data version; `TenantScopedCache` only accepts a `LeagueContext`.
Personalized results are never shared between tenants even for the same public league.

### 4. Provider adapters behind one interface with explicit capabilities
`app/leagues/providers/base.py`, `capabilities.py`

- `LeagueProvider`: discover leagues, settings, teams, rosters, scoring, matchups, standings,
  free agents, transactions, projections; every result is `Sourced` (provider, source, fetch time,
  provider timestamp).
- Each adapter declares every `Capability` as supported / derived / unsupported / requires_auth /
  disabled with a human-readable reason. Asking for an unavailable capability raises
  `CapabilityUnavailable`; adapters never return empty or ESPN-shaped stand-ins.
- Fantasy-league providers are a separate layer from the NFL statistics provider (nflverse).
- Adapters are stateless per tenant; credentials arrive per call and are never cached on them.

### 5. Normalized league model
`app/leagues/models.py`: lineup slots carry explicit eligibility sets (FLEX = RB/WR/TE,
SUPERFLEX = QB/RB/WR/TE; ESPN's `OP` and Sleeper's `SUPER_FLEX` both become SUPERFLEX by
eligibility, not by label). Scoring keeps the provider's rules verbatim, plus the subset mapped
to canonical stat keys and an explicit `unmapped` list; points-per-reception is `None` when a
league does not say, never an assumed default.

### 6. Globally safe player identity
`app/leagues/identity.py`: player ids are namespaced (`sleeper:4046`, `espn:4362628`). A GSIS
link exists only with id-based evidence (published ID map, provider gsis field, the legacy ESPN
map's id-based rows). Name-only matches, including the legacy mapper's `name_position_team`
rows, are kept as unresolved with a reason. `same_athlete` never equates unresolved players.

### 7. End-user authorization is separate from service authentication
`FANTASY_API_KEY` stays as *service* authentication (only our Next.js server may call the API).
End users are identified by a short-lived (≤10 min) HMAC-signed user assertion
(`X-Fantasy-User-Assertion`) minted server-side by Next.js after it authenticates the user. Phase 1
has no identity provider, so in production no assertion is minted and `/v2` treats callers as
anonymous; development may set `FANTASY_DEV_USER_ID`. Choosing the identity provider is a Phase 2
decision (see roadmap).

### 8. Credentials
`app/leagues/credentials.py`: a `CredentialVault` interface with a `DisabledVault` implementation.
Phase 1 stores **no** user credentials: Sleeper needs none, Yahoo is disabled, and ESPN private
access exists only for the deployment owner from server environment variables. Users are never
asked for ESPN cookies. An encrypting vault (envelope encryption with a KMS-held key) is a
prerequisite for Yahoo and is out of scope here.

### 9. Feature flags, default off
`MULTILEAGUE_ENABLED` (API), `MULTILEAGUE_UI_ENABLED` (page), `SLEEPER_SOURCE`
(`disabled`|`fixture`|`live`), `ESPN_PUBLIC_IMPORT_ENABLED`. With everything unset the deployment
behaves exactly as before: `/v2/*` answers 404 and the `/leagues` page explains that the feature is
not enabled.

## Consequences

- The existing single-league endpoints still use the global "latest league" selectors. They are
  safe today only because the owner's database holds one league and one owner reads it. Moving each
  engine (Team Value, Trade Discovery, Waivers, Start/Sit, ...) onto `LeagueContext` is Phase 2 and
  is tracked by the ratchet test; it cannot be done safely in the same PR as the foundation.
- The shared MotherDuck database must not receive data from untrusted users until the analytics
  tables are league-keyed and the hosted read path is scoped (see the migration proposal).
- Live Sleeper import stays off until Sleeper's terms for this use are confirmed; fixture mode lets
  the flow be developed and reviewed without network or licensing exposure.

## Alternatives considered

- **Add `league_id` filters to the existing tables and reuse them for tenants.** Rejected for
  Phase 1: the tables use ESPN-typed ids, the publish path copies everything to one shared
  database, and personalization (`is_mine`) is mixed into league data. Proposed as a staged
  migration instead.
- **Use `FANTASY_API_KEY` + a `user_id` query parameter.** Rejected: any holder of the key could
  impersonate any user by editing the parameter (horizontal privilege escalation).
- **Collect ESPN `espn_s2`/`SWID` from users in a form.** Rejected: these are full ESPN session
  cookies, not scoped tokens; collecting them is credential harvesting with account-takeover
  impact and is not an authorized integration method.
- **Derive the internal league id from provider + external id.** Rejected: guessable and leaks the
  provider league through URLs and logs.

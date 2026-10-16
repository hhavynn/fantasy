# Roadmap: from single-owner dashboard to multi-league platform

Each stage is an independently reviewable PR (or small series). Nothing advances to the next
stage until its exit criteria hold. Live public launch is the last stage, not a byproduct.

## Phase 1: foundation (this PR)
- League context, tenancy store, scoped cache keys, provider interface + capability model,
  normalized league model, id-only player identity, `/v2` API and `/leagues` prototype, all
  behind flags that default off. Sleeper via fixtures; ESPN owner-only; Yahoo disabled.
- Exit: existing suite green; isolation tests green; flags off ⇒ no behaviour change.

## Phase 2a: identity and sessions
- Pick an identity provider (Auth.js with a database adapter, Clerk, Supabase Auth, ...); Next.js
  mints the `X-Fantasy-User-Assertion` from a real session. Remove `FANTASY_DEV_USER_ID` use outside dev.
- Add CSRF/origin checks on mutations, edge rate limiting, an append-only audit log.
- Move the tenancy store to Postgres (row-level security on `user_id`); keep the store interface.
- Exit: penetration test of `/v2` (IDOR, session fixation, assertion replay).

## Phase 2b: league-keyed analytics
- Apply the [schema migration proposal](schema-migration-proposal.md) additively; route the owner
  deployment through a legacy `LeagueContext` shim; convert engines one by one (Team Value, Trade
  Discovery, Waivers, Start/Sit, Matchup, Luck Lab, Playoff Race, Weekly Recap, Managers),
  shrinking the ratchet list in `tests/test_league_scope_audit.py` to zero.
- Scoring-aware projections keyed by a scoring hash.
- Exit: two-league fixtures produce identical per-league results to single-league runs.

## Phase 2c: Sleeper live (read-only)
- Confirm Sleeper's terms for this product and deployment (commercial vs noncommercial), document
  attribution requirements, then enable `SLEEPER_SOURCE=live` in a staging deployment.
- Background sync jobs (queue, per-provider concurrency and backoff, per-league status), the
  players file fetched at most daily and shared.
- Exit: written confirmation of usage rights; load test under Sleeper's rate guidance.

## Phase 3a: Yahoo
- Register and get approval for a Yahoo Developer app; OAuth 2.0 authorization-code flow
  (server-side, `state` + PKCE where supported); encrypted token vault (envelope encryption, KMS
  key, per-tenant data keys); refresh, revocation and deletion; scopes limited to read.
- Exit: security review of the vault; revocation test against Yahoo.

## Phase 3b: ESPN beyond the owner
- ESPN public leagues only if `ESPN_PUBLIC_IMPORT_ENABLED` is cleared by a terms review.
- Private ESPN leagues remain owner-only unless ESPN offers an authorized third-party method.
  We do not collect session cookies from users.

## Phase 4: hosted multi-tenant data plane and public launch
- Replace whole-database MotherDuck publishing for tenant data (per-tenant databases/shares or a
  Postgres/RLS data plane); keep the owner database separate.
- Privacy policy, data retention and deletion (user + league), provider attribution, abuse limits,
  monitoring and on-call.
- Exit: external security review, legal sign-off on provider terms.

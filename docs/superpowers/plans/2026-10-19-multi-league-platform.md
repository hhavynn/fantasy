# Multi-league platform implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let anyone view a public ESPN or Sleeper league, and signed-in users connect private ESPN leagues, on the existing fantasy-intel site, with every page at parity and Los Subies unchanged.

**Architecture:** Supabase Auth + Postgres control plane (RLS, least-privilege roles), FastAPI verifying Supabase JWTs and authorizing every request server-side, one MotherDuck database per league plus a shared read-only `fi_global`, sync as fenced Postgres-queued jobs in Vercel Functions. Spec: [2026-10-19-multi-league-platform-design.md](../specs/2026-10-19-multi-league-platform-design.md).

**Tech Stack:** FastAPI, DuckDB/MotherDuck, Supabase (Auth, Postgres, Supavisor), Next.js on Vercel Pro (Cron, Queues), `cryptography` (AES-256-GCM), PyJWT (JWKS), pytest, Playwright E2E.

## Global Constraints

- Phase N+1 starts only after Phase N's acceptance criteria hold and its PR is merged (Codex review → fix → merge, per the repo's merge process).
- Each phase gets its own detailed task plan (`docs/superpowers/plans/2026-10-19-multi-league-phase-<N>.md`) written when the phase starts; this document fixes boundaries, acceptance and rollback.
- No engine rewrites. Engines change only to (a) receive the perspective team instead of reading `fantasy_teams.is_mine`, and (b) read through the league connection chosen by the request.
- Los Subies golden-response tests must pass at the end of every phase from Phase 2 on.
- Flags default off; with every new flag off, production behaves exactly as today.
- Never deploy the Supabase service-role key; no Postgres role gets `BYPASSRLS`.
- No ESPN cookie is ever logged, returned by an API, or used outside the league read allowlist.
- Ambiguous provider errors never revoke credentials or delete league data.
- MotherDuck is not treated as a per-league security boundary; isolation is server-enforced and leak-tested.

---

## Phase 0: MotherDuck validation spike (go/no-go)

**Detailed plan:** [2026-10-19-multi-league-phase-0-spike.md](2026-10-19-multi-league-phase-0-spike.md)

**Scope:** throwaway measurement harness under `fantasy-intel/spikes/motherduck/`, a throwaway Vercel project `fi-md-spike` for in-region latency, a written report. No production code changes.

**Acceptance (go):**
- Isolation behaviour documented from real SQL attempts with the API's token type; a server-enforced isolation design that does not depend on `attach_mode=single`.
- An affordable mechanism that makes API reads unable to write (or the gap reported as an owner decision).
- `fi_global` attaches read-only beside a league DB and unqualified names resolve via `search_path`.
- From Vercel (production region): connect+attach+first query p95 cold < 1.5 s, warm added p95 < 300 ms.
- Single-league multi-table transaction is atomic under a mid-write kill; the `_sync_meta` fence rejects a stale commit.
- Cross-database snapshot and reader lag measured; status rule (spec 3.7) shown to hold.
- Full Los Subies sync duration measured against Vercel Pro `maxDuration`.
- Monthly cost projection at 100 and 1,000 leagues.
- Vercel Queues (Python) availability determined.

**No-go:** report numbers and blockers to Havyn; revisit Option B vs A/C before any Phase 1 work.

**Rollback:** `uv run python spikes/motherduck/teardown.py` drops every `spike_*` MotherDuck database; delete the `fi-md-spike` Vercel project; nothing else changed.

## Phase 1: Control plane (invisible)

**Scope:** Supabase project; migrations for `app_users`, `leagues`, `league_memberships`, `league_pins`, `team_selections`, `league_invites`, `league_credentials`, `sync_jobs`, `provider_slots`, `league_sync_runs`, `audit_log`; RLS policies; roles `fi_api`/`fi_sync`; SECURITY DEFINER functions (redeem invite, store credential, transfer admin, `sync_credential`); FastAPI JWT verification (JWKS) and `authorize_league(request, league_id) -> Access`; credential crypto module (AES-GCM envelope, AAD, rewrap); ADR-0002. The Phase 1 HMAC assertion code is removed. Nothing user-visible; `/v2` stays behind `MULTILEAGUE_ENABLED`.

**Acceptance:**
- Authorization matrix tests green for every `/v2` league route (router-enumerated).
- RLS tests: `anon` sees only public leagues; `authenticated` sees only own pins/selections/memberships; `fi_api` cannot select `league_credentials`; `fi_sync` cannot read memberships.
- Claims isolation: two interleaved transactions on one pooled connection never see each other's `request.jwt.claims`.
- Crypto: AAD-swap fails, rewrap round-trips, redaction tests green.
- Existing suite + E2E green; production unchanged with flags off.

**Rollback:** flags off (no traffic reaches the new code); Supabase migrations have down-migrations; the Supabase project can be paused. No existing table or route changed.

## Phase 2: League-database read path, Los Subies migrated

**Scope:** golden-response recorder (first task, before any refactor); `fi_global`/league table split and `publish_motherduck.py` publishing `fi_global` only; Los Subies copied into `md:lg_<id>` with `_sync_meta`; request path: authorize → open league cursor (+ `fi_global` read-only, `search_path`, guard from Phase 0) → engines; perspective team passed into engines instead of `is_mine` (engines touched only at that seam); `/l/<lg_id>/…` routes, legacy URL redirect, league switcher, perspective picker (anonymous via URL/local storage), "data as of" from the reader's `_sync_meta`. Laptop sync still writes Los Subies (to its league DB) until Phase 3.

**Acceptance:**
- Golden tests: every legacy GET endpoint byte-identical (volatile fields masked) via legacy URLs and via `/v2/leagues/{lg}/…`, on the E2E fixture in CI and on real Los Subies locally.
- Two-league leak tests green for every route; interleaved-request cache test green.
- Anonymous visitor can view a public fixture league from any perspective without signing in.
- p95 page API latency within +300 ms of today's production.
- `test_league_scope_audit.py` ratchet unchanged or lower.

**Rollback:** `MULTILEAGUE_ENABLED` off routes everything through the legacy single-database path, which stays intact through Phase 5; `md:fantasy_intel` is kept untouched until Phase 5 sign-off, so reverting the publish script restores today's deployment.

## Phase 3: Sync jobs + ESPN connect flows

**Scope:** ESPN sync restructured into fetch → compute → single-transaction commit with fence (parsing/engines unchanged); job worker (claim/reclaim-in-place, heartbeat, provider slots, fence, retries, failure classification per spec 3.6); dispatch via Vercel Queues (or per-minute cron fallback per Phase 0); drain cron (every minute); visibility prober; Connect flow UI (public URL import; private with consent + cookies + team pick); invites; credential lifecycle (`active`/`suspect`/`invalid`); stale/retention timers; Los Subies syncs through jobs with Havyn's private credential; launchd ESPN step removed (global steps remain).

**Acceptance:**
- Sync tests: reclaim in place, heartbeat loss aborts, stale fence rejected, slot cap under 10 concurrent claimers, duplicate enqueue collapses, mid-commit failure leaves data intact, ambiguous 403/404 never invalidates.
- Golden tests still green with Los Subies produced by the job path (compared against the laptop path on the same provider responses).
- End-to-end on staging: connect a public ESPN league → data visible < 2 min; flip it private on ESPN → access 404 within one probe; private connect → invite → viewer access.
- Credentials never appear in logs (log scan in the E2E run).

**Rollback:** disable the drain cron and Queues consumer (flag `SYNC_JOBS_ENABLED`); re-enable the launchd ESPN step; league DBs keep their last committed data.

## Phase 4: Sleeper live + scoring-aware Ours

**Scope:** Sleeper terms check recorded; Sleeper sync job using the existing `app/sleeper/` normalizer into a league DB; Ours re-scored per league scoring from component-stat projections in `fi_global` (QB/RB/WR/TE, K, D/ST) so leagues without ESPN projections get projections; engines read the league's projection source through the existing projection-source seam.

**Acceptance:**
- Re-scored Ours equals today's Ours for Los Subies scoring (golden).
- Every page renders for a Sleeper fixture league with non-ESPN scoring (half-PPR, superflex, TE premium) with no empty or ESPN-shaped stand-ins.
- Leak tests include a Sleeper league.

**Rollback:** `SLEEPER_SOURCE=disabled`; Sleeper leagues show "temporarily unavailable"; ESPN leagues unaffected.

## Phase 5: Parity audit

**Scope:** every page × {ESPN public, ESPN private, Sleeper} checked by E2E; fixes only where a page assumed Los Subies; owner-only research pages gated to platform admins; retire `md:fantasy_intel`.

**Acceptance:** E2E matrix green; no "coming soon" anywhere; Havyn signs off on Los Subies after a week of side-by-side use.

**Rollback:** keep `md:fantasy_intel` and the legacy path until sign-off; reverting is a flag flip.

## Phase 6: Launch gates, then open sign-ups

**Scope:** privacy policy, terms acknowledgement (ESPN's API is undocumented; Sleeper terms) — Havyn's decision recorded; abuse limits (imports per user per day, active leagues per user, anonymous read rate limits); alerts on `dead` jobs and probe failures; account and league deletion flows tested end to end; `SIGNUPS_ENABLED` flag.

**Acceptance:** deletion flows verified (credentials crypto-erased, DB dropped after soft delete); rate limits enforced in tests; alerting fires on an injected dead job; Havyn approves launch.

**Rollback:** `SIGNUPS_ENABLED` off closes registration; public viewing can be closed with `MULTILEAGUE_ENABLED` off.

## Phase 7: Automate global publishing

**Scope:** GitHub Actions scheduled workflow running nflverse sync, the Ours pipeline and `fi_global` publish (atomic replace), plus `recompute` job fan-out; launchd global jobs retired.

**Acceptance:** two consecutive weeks of scheduled publishes with identical outputs to the laptop run; alert on a missed publish.

**Rollback:** re-enable the launchd jobs; disable the workflow.

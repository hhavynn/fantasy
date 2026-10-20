# Multi-league platform implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let anyone view a public ESPN or Sleeper league, and signed-in users connect private ESPN leagues, on the existing fantasy-intel site, with every page at parity, Los Subies unchanged, and everything on free tiers.

**Architecture:** Supabase free (Auth + Postgres with RLS, least-privilege roles); FastAPI on Vercel Hobby verifying Supabase JWTs, authorizing every request, and serving from immutable per-league DuckDB files cached in `/tmp`; files live in a private Cloudflare R2 bucket (API holds a read-only token); sync runs as fenced Postgres-queued jobs in GitHub Actions. Spec: [2026-10-19-multi-league-platform-design.md](../specs/2026-10-19-multi-league-platform-design.md) (revised 2026-10-20 for free tiers).

**Tech Stack:** FastAPI, DuckDB, Cloudflare R2 (S3 API, SigV4), Supabase (Auth, Postgres, Supavisor), Next.js on Vercel Hobby, GitHub Actions, `cryptography` (AES-256-GCM + RSA-OAEP), PyJWT (JWKS), pytest, Playwright E2E.

## Global Constraints

- Free tiers only. A step that needs a paid feature stops and is raised with Havyn with a free alternative.
- Phase N+1 starts only after Phase N's acceptance criteria hold and its PR is merged (Codex review → fix → merge, per the repo's merge process).
- Each phase gets its own detailed task plan (`docs/superpowers/plans/2026-10-19-multi-league-phase-<N>.md`) written when the phase starts; this document fixes boundaries, acceptance and rollback.
- No engine rewrites. Engines change only to (a) receive the perspective team instead of reading `fantasy_teams.is_mine`, and (b) read through the connection chosen by the request.
- Los Subies golden-response tests must pass at the end of every phase from Phase 1a on.
- Flags default off; with every new flag off, production behaves as the previous phase left it.
- Never deploy the Supabase service-role key; no Postgres role gets `BYPASSRLS`.
- The API holds only a read-only R2 token and only the credential *public* key.
- No ESPN cookie is ever logged, returned by an API, or used outside the league read allowlist. GitHub Actions logs are public: workers log ids, durations and error codes only.
- Ambiguous provider errors never revoke credentials or delete league data.

---

## Phase 0 (retired): MotherDuck spike

Done 2026-10-20 ([report](../../../fantasy-intel/spikes/motherduck/REPORT.md)). MotherDuck is dropped for cost; the `spike_*` databases and the `fi-md-spike` Vercel project are torn down in Phase 1a once the new read path is live.

## Phase 0b: object storage spike

**Scope:** benchmark private Vercel Blob, Supabase Storage and Cloudflare R2 from a Vercel Function in `iad1` with the real snapshot (95 MB full; 31 MB league + 47 MB global split): full download + open + an 8-query page mix, and DuckDB HTTP range reads; record free-tier budgets. Report in `fantasy-intel/spikes/storage/REPORT.md`.

**Acceptance:** one store meets p95 < 2 s for the 95 MB download from `iad1` with no transfer charge, and supports a read-only credential.

**Status:** done 2026-10-20 — R2 chosen; single-stream R2 misses the target (p95 4.0 s), parallel ranged GETs meet it (p95 ~1.0–1.4 s), so the client uses 8 ranges. Blob and Supabase are ruled out by free-tier transfer/file-size limits.

**Rollback:** delete the `spike` objects/bucket in each store and the `fi-spike-blob` Blob store.

## Phase 1a: move the current site off MotherDuck

**Scope:** `backend/app/db/snapshot.py` (R2 SigV4 GET/PUT with the standard library, manifest, `/tmp` cache with lock and atomic rename); `api/deps.py` serves from the snapshot when `SNAPSHOT_BUCKET` is set; `scripts/publish_snapshot.py` builds a compact copy (`COPY FROM DATABASE`), uploads `snapshots/<stamp>.duckdb`, then `current.json`, keeps the last 3; `refresh_all.py` publishes the snapshot instead of MotherDuck; Vercel env on `fantasy-intel-api`: `SNAPSHOT_*` + read-only R2 token, `MOTHERDUCK_*` removed after verification; README hosting section updated.

**Acceptance:**
- Unit tests: manifest parsing, cache hit/miss, concurrent requests download once, atomic replace, corrupt/partial download rejected (size + sha256), read-only token never used for writes, SigV4 signing matches AWS's published test vector.
- Golden check: every page's API responses from the snapshot path equal the MotherDuck path on the same publish.
- Production: cold request p95 < 3 s (download + open), warm unchanged; `published_at` data version flows to the trade-search cache.
- Free-tier check after a week: R2 reads ≪ 10M, Vercel Active CPU on track for < 4 h/month.

**Rollback:** unset `SNAPSHOT_BUCKET` on Vercel → the API falls back to `MOTHERDUCK_DATABASE` (kept until MotherDuck's downgrade; after that, redeploy the previous commit).

## Phase 1: Control plane (invisible)

**Scope:** Supabase project `fantasy-intel` (created 2026-10-20, us-east-1); migrations for `app_users`, `leagues`, `league_memberships`, `league_pins`, `team_selections`, `league_invites`, `league_credentials`, `sync_jobs`, `provider_slots`, `league_snapshots`, `league_snapshot_history`, `audit_log`; RLS policies; roles `fi_api`/`fi_sync`; SECURITY DEFINER functions (redeem invite, store credential, transfer admin, `sync_credential`); FastAPI JWT verification (JWKS) and `authorize_league(request, league_id) -> Access` returning the snapshot pointer; credential crypto (AES-GCM data key, RSA-OAEP wrap, AAD, rewrap); ADR-0002. The HMAC assertion code is removed. Nothing user-visible; `/v2` stays behind `MULTILEAGUE_ENABLED`.

**Acceptance:**
- Authorization matrix tests green for every `/v2` league route (router-enumerated).
- RLS tests: `anon` sees only public leagues; `authenticated` sees only own pins/selections/memberships; `fi_api` cannot select `league_credentials`; `fi_sync` cannot read memberships.
- Claims isolation: two interleaved transactions on one pooled connection never see each other's `request.jwt.claims`.
- Crypto: AAD-swap fails, the API (public key only) cannot decrypt, rewrap to a new keypair leaves the cookie ciphertext and AAD untouched and it still decrypts, redaction tests green.
- Existing suite + E2E green; production unchanged with flags off.

**Rollback:** flags off; Supabase migrations have down-migrations. No existing table or route changed.

## Phase 2: League-file read path, Los Subies migrated

**Scope:** golden-response recorder (first task); global/league table split; publisher writes `global/<version>.duckdb` and Los Subies `leagues/<lg_id>/<version>.duckdb` and updates `league_snapshots`; request path: authorize → pointer → `/tmp` LRU (≤ 350 MB) → open league read-only + attach global read-only + `search_path` → engines; perspective team passed in; `/l/<lg_id>/…` routes, legacy URL redirect, league switcher, perspective picker (anonymous via URL/local storage); "data as of" from the served version; per-instance response cache keyed by `(league, data_version, global_version, visibility_epoch, perspective, route, params)` after authorization.

**Acceptance:**
- Golden tests: every legacy GET endpoint byte-identical (volatile fields masked) via legacy URLs and `/v2/leagues/{lg}/…`, on the E2E fixture in CI and on real Los Subies locally.
- Two-league leak tests green for every route; interleaved-request cache test green.
- Anonymous visitor can view a public fixture league from any perspective without signing in.
- `/tmp` LRU never exceeds its bound under a 20-league request mix; concurrent first requests download once.
- `test_league_scope_audit.py` ratchet unchanged or lower.

**Rollback:** `MULTILEAGUE_ENABLED` off routes everything through the Phase 1a single-snapshot path, which stays intact through Phase 5.

## Phase 3: Sync jobs on GitHub Actions + ESPN connect flows

**Scope:** worker package run by `.github/workflows/sync-drain.yml` (`schedule: */15`, `repository_dispatch`, `workflow_dispatch`; concurrency group; 20-minute budget); claim/reclaim-in-place, heartbeat, provider slots, fence (publish = fenced upsert, so a new league's first publish inserts its pointer), retries, failure classification (spec 3.6); league file built on the runner from the existing `fetch_all`/`write_all` + analytics, uploaded, fenced pointer swap; raw provider JSON stored for `recompute`; garbage collection of orphaned objects; visibility prober; API `repository_dispatch` on connect; Connect flow UI (public URL import; private with consent + cookies + team pick); invites; credential lifecycle; stale/retention timers; Los Subies syncs through jobs with Havyn's private credential; launchd ESPN step removed (global steps remain).

**Acceptance:**
- Sync tests: reclaim in place, heartbeat loss aborts, stale fence publishes nothing, a new league's first publish becomes visible, racing first publishes keep the higher fence, slot cap under 10 concurrent claimers, duplicate enqueue collapses, failure before the swap leaves the pointer unchanged, ambiguous 403/404 never invalidates.
- Golden tests still green with Los Subies produced by the job path.
- Public-log audit: a full drain run's log contains no team names, player names, cookies or league ids beyond the opaque `lg_` id (automated grep in the workflow).
- End to end: connect a public ESPN league → data visible once the dispatch run finishes (target < 5 min); flip it private on ESPN → access 404 within one probe; private connect → invite → viewer access.

**Rollback:** disable the workflow; re-enable the launchd ESPN step publishing Los Subies; league files keep their last version.

## Phase 4: Sleeper live + scoring-aware Ours

**Scope:** Sleeper terms check recorded; Sleeper sync job using the existing `app/sleeper/` normalizer into a league file; Ours re-scored per league scoring from component-stat projections in the global file (QB/RB/WR/TE, K, D/ST) so leagues without ESPN projections get projections; engines read the league's projection source through the existing projection-source seam.

**Acceptance:** re-scored Ours equals today's Ours for Los Subies scoring; every page renders for a Sleeper fixture league with non-ESPN scoring (half-PPR, superflex, TE premium) without empty or ESPN-shaped stand-ins; leak tests include a Sleeper league.

**Rollback:** `SLEEPER_SOURCE=disabled`; Sleeper leagues show "temporarily unavailable"; ESPN leagues unaffected.

## Phase 5: Parity audit

**Scope:** every page × {ESPN public, ESPN private, Sleeper} checked by E2E; fixes only where a page assumed Los Subies; owner-only research pages gated to platform admins; retire the Phase 1a single-snapshot path.

**Acceptance:** E2E matrix green; no "coming soon" anywhere; Havyn signs off on Los Subies after a week of side-by-side use.

**Rollback:** keep the Phase 1a path until sign-off; reverting is a flag flip.

## Phase 6: Launch gates, then open sign-ups

**Scope:** privacy policy, terms acknowledgement (ESPN's API is undocumented; Sleeper terms; Vercel Hobby non-commercial) — Havyn's decision recorded; abuse limits (imports per user per day, active leagues per user, anonymous read rate limits); free-tier usage dashboard + alerts at 70% of each budget (Vercel Active CPU, R2 ops/storage, Supabase DB/egress); alerts on `dead` jobs; deletion flows tested; `SIGNUPS_ENABLED` flag.

**Acceptance:** deletion flows verified (credentials crypto-erased, league objects deleted after soft delete); rate limits enforced in tests; usage alert fires on an injected threshold; Havyn approves launch.

**Rollback:** `SIGNUPS_ENABLED` off closes registration; public viewing can be closed with `MULTILEAGUE_ENABLED` off.

## Phase 7: Automate global publishing

**Scope:** GitHub Actions scheduled workflow running nflverse sync, the Ours pipeline and the global-file publish, plus `recompute` job fan-out; launchd global jobs retired.

**Acceptance:** two consecutive weeks of scheduled publishes with outputs identical to the laptop run; alert on a missed publish.

**Rollback:** re-enable the launchd jobs; disable the workflow.

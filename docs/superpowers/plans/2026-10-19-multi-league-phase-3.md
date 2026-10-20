# Multi-league Phase 3: Sync jobs on GitHub Actions + ESPN connect flows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** League files are built and published by fenced, Postgres-queued sync jobs that run in GitHub Actions, and people can sign in, import a public ESPN league, connect a private one with their cookies, and invite viewers. Los Subies' ESPN data syncs through jobs with Havyn's private credential.

**Architecture:** `fi.sync_jobs` stays the authoritative queue. A drain (`scripts/sync_drain.py`, run by `.github/workflows/sync-drain.yml` every 15 minutes and on `workflow_dispatch`) does the following:
- claims jobs in one transaction: candidate row, per-league lock, provider slot, fence;
- heartbeats every 30 s;
- builds the league file on the runner: current global file plus the league's previous file, then ESPN `fetch_all` → `write_all` → `run_analytics`, then `split_database`;
- uploads it and moves the pointer with the spec's fenced upsert, using the job's fence;
- records the outcome per the spec's failure table.

The API registers leagues and stores credentials through SECURITY DEFINER functions. It enqueues jobs and triggers the workflow with a token that can only dispatch Actions. Next.js gains Supabase sign-in (email one-time code) and the Connect, invite and credential pages.

**Tech Stack:** Python 3.12, psycopg 3 (`fi_sync`), DuckDB, `requests` + `espn_api`, `cryptography` (existing `credential_crypto`), GitHub Actions, Supabase Auth (`@supabase/ssr`), Next.js 16, pytest, node --test, Playwright.

Master plan: [2026-10-19-multi-league-platform.md](2026-10-19-multi-league-platform.md). Spec: [2026-10-19-multi-league-platform-design.md](../specs/2026-10-19-multi-league-platform-design.md) §2–§3. Previous phases: [phase-1](2026-10-19-multi-league-phase-1.md), [phase-2](2026-10-19-multi-league-phase-2.md).

## Global Constraints

- Free tiers only: standard GitHub-hosted runners on this public repo, R2 free, Supabase free, Vercel Hobby. Supabase Auth's built-in email sender (a few emails per hour) is enough until sign-ups open (Phase 6 decides custom SMTP).
- Flags default off.
  - `MULTILEAGUE_ENABLED`, `MULTILEAGUE_UI_ENABLED`: from earlier phases.
  - `MULTILEAGUE_CONNECT_ENABLED` (API): import, connect, invite and credential routes.
  - `SYNC_DRAIN_ENABLED` (repository variable read by the workflow): with it unset, the scheduled drain exits at once.
  - With every flag off, production behaves as Phase 2 left it.
- The workflow runs only on `schedule` and `workflow_dispatch`, never on `pull_request`. Its secrets are:
  - `FI_SYNC_DB_URL`;
  - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (an Object Read & Write token for the `fantasy-intel` bucket);
  - `CREDENTIALS_PRIVKEY_V1`.

  The Supabase service-role key is never used.
- Public logs. The drain prints only opaque ids (`lg_…`, job uuids), durations, counts and error codes. Every line goes through a redacting printer that drops any line containing a sensitive string seen in the run: team, member and player names, the ESPN league id, cookies. Decrypted cookies are `::add-mask::`ed before use.
- ESPN cookies are used only for GETs whose URL is under `…/seasons/<season>/segments/0/leagues/<id>` or `…/leagueHistory/<id>` for that league. Every other ESPN request (pro players, the control request) is sent without cookies.
- Ambiguous provider errors never revoke credentials or delete data. Only the spec §3.6 confirmation rule (≥3 authenticated failures over ≥6 h, with a successful control request in the same run) marks a credential `invalid` or a league `unavailable`.
- The only step that makes data visible is the fenced upsert (`… WHERE fi.league_snapshots.fence < EXCLUDED.fence`), carrying the fence the job got at claim time.
- No engine rewrites. The runner calls the existing `fetch_all`, `write_all` and `run_analytics` unchanged.

## Decisions taken in this plan

| Topic | Decision | Why |
|---|---|---|
| Two PRs | **3a** (Tasks 1–9): queue, worker, workflow, global pointer, Los Subies job path. **3b** (Tasks 10–15): migrations and API for import, connect, invites and credentials; sign-in; Connect UI; end to end. Both live under this plan. | Each PR is reviewable and has its own tests; Phase 4 starts only after 3b is merged and every Phase 3 acceptance criterion holds |
| What a job builds from | Working DB = a copy of the current global file + the league's previous league file (tables the job does not rebuild carry forward) + fresh ESPN data + `run_analytics`, then `split_database`. The runner's split global is discarded; the pointer names the global version the job read | Same engines and tables as the laptop path. The laptop's Ours, kicker, D/ST and report tables survive job publishes until Phase 4 moves Ours to the runner |
| Current global version | New single-row table `fi.global_snapshot(global_version, committed_at)`, written by the laptop publisher (`fi_sync`). Jobs read it | A job must know which global file to build against; Phase 7 moves global publishing to Actions |
| Los Subies | Synced by jobs with Havyn's private credential (connected in 3b). The laptop keeps publishing Los Subies after its own refresh until Phase 4 (both publishers are fenced) | The laptop's Ours and research steps read its local ESPN data. Removing the launchd ESPN step moves to Phase 4, together with Ours |
| Workflow trigger from the API | `workflow_dispatch` with a fine-grained token that has **Actions: write** only (`GITHUB_DISPATCH_TOKEN` on Vercel) | `repository_dispatch` needs Contents: write, which could also push code |
| Parallelism | One drain run at a time (`concurrency: sync-drain`, not cancelled); inside a run, two worker processes drain the queue. `provider_slots` (ESPN 2, Sleeper 4) cap provider load | GitHub keeps one pending run per group; slots are the race-safe cap |
| Raw provider data | `raw/<lg>/<data_version>.json.gz` in R2 (member SWIDs scrubbed, as `save_raw` does). `recompute` jobs rebuild from it without fetching | Spec §3.7; same private bucket |
| Control request (§3.6) | Cookie-less GET of ESPN's season `players` endpoint (public) | Proves ESPN is reachable without touching another league |
| Sign-in | Supabase email one-time code via `@supabase/ssr`; the Next.js server forwards the access token as `Authorization: Bearer` | Free; no OAuth app to create. FastAPI already verifies these JWTs (Phase 1) |
| Timers | The drain's maintenance step (once per run) marks dormant leagues, expires stale and private-flipped leagues, finishes soft deletes and enqueues due refreshes and probes | No extra infrastructure; runs every 15 min |

## File structure

| File | Responsibility |
|---|---|
| `supabase/migrations/20261021120000_sync_queue.sql` (+ `down/`) | `fi.global_snapshot`; `leagues.status_changed_at`; `fi_sync` grants for maintenance (`name`, `deleted_at`, `status_changed_at`); `fi.retire_league_objects_due()` view for the drain |
| `supabase/migrations/20261021130000_connect_flows.sql` (+ `down/`) | `fi.import_public_league`, `fi.connect_private_league`, `fi.request_sync`, `fi.request_probe`, `fi.delete_league` (admin soft delete) |
| `backend/app/sync/queue.py` | `enqueue`, `claim`, `heartbeat`, `complete`, `fail`, `release_slot`: the §3.3–3.4 SQL |
| `backend/app/sync/failures.py` | Pure classification: an exception and its context → `Retry(delay)`, `Dead`, `Suspect`, `ConfirmInvalid`, `ConfirmUnavailable`, `Restrict` |
| `backend/app/sync/espn_source.py` | Builds an `EspnClient` with cookies scoped to the league URL allowlist; the cookie-less control and visibility probes |
| `backend/app/sync/raw_store.py` | `EspnPayloads` ⇄ gzip JSON (`dump_payloads`, `load_payloads`) |
| `backend/app/sync/build.py` | `build_league_file(...)`: working DB from global + previous league file, ESPN write, analytics, split |
| `backend/app/sync/publish.py` | `publish_built(...)`: upload the league object and raw payloads, fenced upsert with the job's fence, history, collect |
| `backend/app/sync/redact.py` | `Printer`: the only output path of the drain; drops lines containing sensitive strings |
| `backend/app/sync/worker.py` | `run_job(...)` per kind (initial/scheduled/manual/recompute/probe), heartbeat thread, credential decrypt |
| `backend/app/sync/maintenance.py` | Scheduled and probe enqueueing, dormancy, retention, soft-delete completion |
| `scripts/sync_drain.py` | CLI: N worker processes, 20-minute budget, maintenance once |
| `.github/workflows/sync-drain.yml` | Schedule, `workflow_dispatch`, concurrency, secrets, masked env, log audit |
| `backend/app/db/league_publish.py` | `publish_league` (laptop) also writes `fi.global_snapshot` |
| `backend/app/api/connect.py` | 3b: `/v2/leagues/import`, `/v2/leagues/connect`, invites, credential, `/sync` routes; `dispatch_drain()` |
| `frontend/lib/supabase/*.ts`, `frontend/app/login/*` | 3b: sign-in |
| `frontend/app/leagues/*`, `frontend/app/l/[league]/settings/*` | 3b: Connect, invites, credentials, sync status |

## Tasks

The task sections below are appended as each task lands, with the shipped files verbatim (the same convention as Phase 2).

**3a**
1. Migration: global pointer, status timestamps, `fi_sync` maintenance grants.
2. `app/sync/queue.py`. Tests: reclaim in place; heartbeat loss; slot cap under 10 concurrent claimers; duplicate enqueue collapses; backoff schedule; `dead` after 5 attempts.
3. `app/sync/failures.py`. Tests: every row of spec §3.6; the confirmation rule (3 failures over 6 h *and* a control success).
4. `app/sync/espn_source.py` + `raw_store.py`. Tests: the cookie allowlist refuses other URLs; the control request carries no cookies; payloads round-trip; SWIDs are scrubbed.
5. `app/sync/build.py`. Tests: the fixture league built by a job equals the laptop path's league file table by table; previous-version tables carry forward; a failure leaves nothing.
6. `app/sync/publish.py` + `league_publish` global pointer. Tests: stale fence publishes nothing; a new league's first publish is visible; racing first publishes keep the higher fence; a failure before the swap leaves the pointer unchanged.
7. `app/sync/redact.py` + `app/sync/worker.py`. Tests: a full drain of the fixture prints no team, member or player name, ESPN id or cookie; lease loss aborts without publishing; a 401 becomes `suspect`, never `invalid`, without confirmation.
8. `app/sync/maintenance.py`. Tests: scheduled enqueue skips dormant leagues; probe due after 6 h; private probe restricts at once (`visibility_epoch + 1`); dormancy and retention timers; soft delete finished after 7 days.
9. `scripts/sync_drain.py` + `.github/workflows/sync-drain.yml`. Tests: the drain CLI on the fixture end to end (DirStore + local Postgres); the workflow has no `pull_request` trigger and masks secrets (static check).

**3b**
10. Migration: connect functions.
11. `app/api/connect.py`. Tests: authorization matrix for the new routes; cookies validated by one fetch and never echoed; invite create, redeem and revoke; dispatch called after commit and its failure tolerated.
12. Frontend sign-in (`@supabase/ssr`). Tests: unit (session → bearer), E2E with the flag off unchanged.
13. Frontend Connect, invites, credentials and sync status. Tests: unit + Playwright against a mocked API.
14. Los Subies through jobs: runbook to connect the credential, a local golden check against the job-built file.
15. End to end on production: public import visible within 5 min; flip private → 404 within one probe; private connect → invite → viewer.

## Acceptance (master plan, Phase 3)

- Sync tests: reclaim in place, heartbeat loss aborts, a stale fence publishes nothing, a new league's first publish becomes visible, racing first publishes keep the higher fence, the slot cap holds under 10 concurrent claimers, duplicate enqueue collapses, a failure before the swap leaves the pointer unchanged, an ambiguous 403/404 never invalidates.
- Golden tests still green with Los Subies produced by the job path.
- Public-log audit: a full drain run's log contains no team names, player names, cookies or league ids beyond the opaque `lg_` id (automated in the workflow and in tests).
- End to end: connect a public ESPN league → data visible once the dispatched run finishes (target < 5 min); flip it private on ESPN → access 404 within one probe; private connect → invite → viewer access.

## Turning it on (production)

Each step is reversible. With a step undone, the flags it gates stay off.

1. **Supabase.**
   - Apply `20261021120000_sync_queue.sql` and `20261021130000_connect_flows.sql`.
   - Check the security advisors.
   - Havyn gives `fi_api` and `fi_sync` their LOGIN passwords (`supabase/README.md`).
   - In Auth, set the Site URL and add `<site>/auth/callback` (and `http://localhost:3000/auth/callback`) to the redirect URLs.
   - Havyn creates their own user under Authentication, then Users. Sign-ups stay closed until Phase 6.
2. **GitHub.**
   - Secrets: `FI_SYNC_DB_URL`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (Object Read & Write on `fantasy-intel`) and `CREDENTIALS_PRIVKEY_V1`.
   - Repository variable: `SYNC_DRAIN_ENABLED=true`.
3. **Vercel API (`fantasy-intel-api`).**
   - `SUPABASE_URL`, `SUPABASE_DB_URL` (fi_api) and `CREDENTIALS_PUBKEY_V1`.
   - `GITHUB_DISPATCH_TOKEN`: fine-grained, this repository, Actions: write only.
   - Then `MULTILEAGUE_ENABLED=1` and `MULTILEAGUE_CONNECT_ENABLED=1`.
4. **Vercel web.**
   - `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` (the publishable key; never the secret or service-role key).
   - `SITE_URL`, then `MULTILEAGUE_UI_ENABLED=1`.
5. **Los Subies.**
   - Havyn signs in once, and the runbook's admin and perspective rows are applied (`supabase/runbook-los-subies.sql`).
   - Havyn stores their cookies on `/leagues/lg_KxH_Y1Y56aM7qnCCseKvs1sU`, which queues a manual sync.
6. **The laptop.**
   - `LEAGUE_PUBLISH_ID=lg_KxH_Y1Y56aM7qnCCseKvs1sU` and `FI_SYNC_DB_URL` in `.env`, so `refresh_all` keeps publishing the global file and Los Subies' research tables.

## Rollback

- Unset `SYNC_DRAIN_ENABLED` (or disable the workflow) and `MULTILEAGUE_CONNECT_ENABLED`. League files keep their last version, and the laptop keeps publishing Los Subies as in Phase 2.
- Down-migrations restore the Phase 2 schema.

# Multi-league platform: design

- Status: approved design (2026-10-19), Option B pending the Phase 0 MotherDuck spike
- Supersedes for the hosted product: parts of [ADR-0001](../../multileague/ADR-0001-league-context-and-tenancy.md)
  (custom user assertion, "never collect ESPN cookies") and the [roadmap](../../multileague/roadmap.md) phases 2–4.
  An ADR-0002 recording these changes lands with Phase 1.
- Plan: [2026-10-19-multi-league-platform.md](../plans/2026-10-19-multi-league-platform.md)

## Goal

Make the existing fantasy website usable by anyone for their own league, not only for Los Subies,
without rebuilding features that already work. Anyone can view a public league; signed-in users can
save preferences and connect private ESPN leagues; Sleeper leagues work too; every page reaches
parity before sign-ups open.

## Decisions (from brainstorming)

| Topic | Decision |
|---|---|
| Audience | Anyone on the internet |
| ESPN access | Public leagues by default (no cookies); opt-in private leagues with user-pasted `espn_s2`/`SWID`, encrypted |
| Other providers | Sleeper (public API). Yahoo out of scope |
| Accounts | Supabase Auth (Google + email magic link) |
| Control plane | Supabase Postgres with RLS |
| Sync host | Vercel Functions (team is on Vercel **Pro**, so per-minute cron is available) |
| League data isolation | **Option B**: one MotherDuck database per league + one shared global database |
| Launch scope | Full parity on every page before sign-ups open |
| Global data | Laptop keeps publishing `fi_global` for now; automated later (Phase 7) |
| UX | Preserve the current site; add sign-in, a league switcher, a perspective picker and a Connect flow |

## 1. Architecture

```
Browser ── Next.js (Vercel) ── Supabase Auth (@supabase/ssr session cookie)
              │  forwards Authorization: Bearer <Supabase access token> (when signed in) + X-API-Key
              ▼
         FastAPI (Vercel) ── verifies the Supabase JWT itself (JWKS, cached)
              │            ── Supabase Postgres (control plane, RLS enforced for request traffic)
              ▼
         MotherDuck
           md:fi_global      ← laptop publishes (Phase 7 automates it)
           md:lg_<opaque id> ← one per league, written only by sync jobs
```

- **Auth.** FastAPI verifies Supabase access tokens directly: signature against the project's JWKS
  (`/auth/v1/.well-known/jwks.json`, cached, refreshed on unknown `kid`), `iss`, `aud=authenticated`,
  `exp`; the user id is `sub`. The Phase 1 HMAC user assertion is removed. `FANTASY_API_KEY` remains
  service authentication (only our Next.js server calls the API). No token = anonymous.
- **Authorization** is decided in FastAPI from Postgres for every request, before any league database
  is opened (Section 2). RLS is a second line of defense, not the only check.
- **Read path.** After authorization, a cursor on `md:lg_<id>` with `fi_global` attached read-only and
  `search_path = lg_<id>.main, fi_global.main`; the existing engines run with the request's
  *perspective team* passed in instead of reading `fantasy_teams.is_mine`.
- **MotherDuck is not a per-league security boundary.** MotherDuck documents no per-token database
  restriction inside an account; `attach_mode=single` only sets the initial context. Cross-league
  isolation is enforced by our server: the database name is derived only from an authorized
  league id, no user-supplied SQL exists, and the leak tests in Section 5 cover every route. What
  credentials *can* enforce (read-only reads, a reader identity that cannot write) is measured in
  Phase 0.
- **Global data.** `fi_global` holds league-independent data: nflverse, NFL schedule/context, Ours
  component-stat projections, research/evaluation tables. The laptop publishes it (atomic
  replace) until Phase 7.
- **UX.** Same pages, components and engines. Additions: sign-in/account menu, league switcher,
  perspective picker, Connect flow. Current URLs keep working (redirect to the viewer's default
  league).

## 2. Access model and lifecycle

### Four separate concepts

| Concept | Storage | Rule |
|---|---|---|
| Visibility | `leagues.visibility` ∈ `public`/`private`, `visibility_checked_at`, `visibility_epoch` | A public league is viewable by anyone, signed in or not. No connection or membership is required |
| Membership | `league_memberships(user_id, league_id, role ∈ {admin, viewer}, created_at, revoked_at)` | Required only for private leagues. App roles are ours and are unrelated to ESPN commissioner status |
| Credential/sync ownership | `league_credentials(id, league_id, owner_user_id, status, ciphertext…)` | Private ESPN only. One active credential per league; used only by sync jobs |
| Perspective (personal team) | anonymous: URL `?team=` + local storage; signed in: `team_selections(user_id, league_id, team_external_id)` | A preference only. Never verified, never grants anything. Any viewer, including anonymous, may analyse a league from any roster |

Pins: `league_pins(user_id, league_id)` put a league in a signed-in user's switcher.

### Public leagues are platform-managed
- Importing a public league registers it once (`unique(provider, external_id, season)`) and pins it.
  Unpinning removes only the pin. No user can delete a public league's data.
- A league with no pins and no views for 30 days becomes `dormant` (no scheduled sync). 90 days
  dormant → database dropped; re-import rebuilds it from the provider.
- Platform admins (`app_users.is_platform_admin`) can take a league down (abuse/legal).

### Private leagues
- The connecting user becomes `admin` and the credential owner.
- **Admin transfer:** an admin promotes another member; the last admin cannot be demoted.
- **Credential handover:** credentials are personal and never transferred. A member attaches their own
  credential only when no active credential exists, or when the current owner explicitly hands over;
  the old credential is destroyed in the same transaction.
- **Membership via own cookies:** a user whose own ESPN cookies show their SWID owning a team in the
  league becomes a `viewer`. This never grants `admin` and never replaces the active credential.
- **Invites:** 128-bit random token shown once; only SHA-256(token) stored; single use; 7-day expiry;
  revocable; viewer-only; redemption requires sign-in; audit-logged.
- **Credential lifecycle:** `active` → `suspect` (ambiguous failures, sync retrying) → `invalid`
  (confirmed, Section 3.6) or back to `active`. `invalid` or owner-revoked → league `stale`: sync
  paused, members keep read access to the last data with an "as of …, sync paused" banner, admins
  notified. Credentials are never auto-deleted on provider errors; the owner re-enters or deletes them.
- **Retention:** a private league stays `stale` with no active credential for 60 days → deleted, with an
  in-app warning at day 30. Admin delete = 7-day soft delete, then the database drops.
- **Account deletion:** the user's credentials are crypto-erased immediately (ciphertext and wrapped
  key deleted); memberships, pins, selections and unredeemed invites removed. If they held the active
  credential the league goes `stale` (rules above). If they were the last admin, the longest-standing
  member becomes admin and is notified; no members left → league deleted. Public leagues are
  unaffected.
- **Audit log:** append-only metadata (actor, action, target, time), no league data, kept 1 year.

### Dynamic visibility (fail closed)
- Public ESPN leagues are probed **without cookies** at every sync and at least every 6 h; Sleeper
  leagues are probed to detect deletion.
- Public access is valid while `visibility_checked_at` is younger than 24 h. Expired → non-members get
  404 until a fresh probe succeeds; the first viewer after expiry triggers a probe (≤1 per league per
  5 min); probe timeout/error keeps it closed.
- A probe that sees the league as private restricts it immediately (`visibility = private`,
  `visibility_epoch + 1`) in one transaction. A public-import league with no credential becomes
  unavailable to everyone ("now private on ESPN; ask a league member to connect it privately"); data
  is kept 30 days in case it flips back. Restricting is the safe direction, so a single private
  signal suffices; restoring public access needs a successful anonymous fetch.
- Authorization runs before every cache lookup; cache keys include `visibility_epoch`; league
  responses are `Cache-Control: private, no-store`.

### Credential security
- AES-256-GCM envelope encryption (`cryptography`): a random 256-bit data key per credential encrypts
  `{"espn_s2","swid"}`; the data key is wrapped by `CREDENTIALS_KEK_v{n}` (Vercel env, sync project
  only). AAD = `league_id ‖ credential_id ‖ owner_user_id ‖ key_version`, so a ciphertext moved to
  another row fails to decrypt. 96-bit random nonces.
- Rotation: add `KEK_v{n+1}`, a `rewrap` job unwraps/rewraps data keys only (cookies never decrypted),
  then the old KEK is removed.
- ESPN cookies are account-wide session cookies. They are used only for GETs to ESPN's fantasy read
  endpoint for that league and season (URL allowlist enforced in the sync client); never for writes,
  other leagues or other ESPN endpoints; never logged, returned or included in errors (redaction
  tests). Validated by one fetch at submission. Connecting requires explicit consent text: they grant
  access to the user's ESPN account; signing out of ESPN everywhere revokes them.

### Postgres roles (least privilege, no `BYPASSRLS`)
- `fi_api` (request path): `NOINHERIT`, member of `authenticated` and `anon`. Each request transaction
  runs `SET LOCAL ROLE authenticated|anon` and `set_config('request.jwt.claims', <verified claims>,
  true)`, so RLS and `auth.uid()` apply. No grants on `league_credentials`. Mutations that need more
  than RLS allows (redeem invite, store credential, transfer admin) go through `SECURITY DEFINER`
  functions that check `auth.uid()` themselves, with `search_path` pinned.
- `fi_sync` (sync jobs): no `BYPASSRLS`. Table grants limited to `leagues`, `sync_jobs`,
  `provider_slots`, `league_sync_runs`; RLS policies `TO fi_sync` on those tables. Credentials are read
  only through `SECURITY DEFINER sync_credential(job_id, lease_token)`, which returns ciphertext only for
  a league whose job is currently leased with that token.
- The Supabase service-role key is never deployed to either project.
- Transaction-local claims are verified not to leak across pooled connections (Supavisor transaction
  mode) in Phase 1 tests.

## 3. Sync jobs

Postgres is the authoritative queue. No extra infrastructure.

### 3.1 Tables
- `sync_jobs(id, league_id, kind ∈ {initial, scheduled, manual, probe, recompute, rewrap}, status ∈
  {queued, running, succeeded, failed, dead}, attempt, max_attempts=5, run_after, lease_owner,
  lease_token, lease_expires_at, heartbeat_at, provider_slot, idempotency_key, error_code,
  error_detail (redacted), fi_global_version, created_at, started_at, finished_at)`.
- `leagues.sync_fence BIGINT` — monotonic, incremented on every claim or reclaim of a job for that league.
- `provider_slots(provider, slot_no, job_id NULL, lease_expires_at)` — N rows per provider (ESPN 2,
  Sleeper 4).

### 3.2 Dispatch
- Connecting a league inserts the `initial` job and commits. Then the request dispatches it via
  Vercel Queues (`vercel-queue`, at-least-once delivery) and awaits the publish. A failed publish
  is logged; the job row still exists.
- Backstop: Vercel Cron calls `/internal/sync/drain` **every minute** (Pro). The drain enqueues due
  scheduled refreshes (staggered per league, more often on game days) and claims queued jobs.
- Phase 0 verifies Vercel Queues for Python on this team. If it is unavailable, the per-minute cron
  alone dispatches (initial sync starts within ~60 s) and the UI shows "queued".

### 3.3 Claim, lock, lease, fence (one Postgres transaction)
1. Pick a candidate: `status='queued' AND run_after <= now()`, or `status='running' AND lease_expires_at <
   now()` (a crashed worker), ordered by `run_after`, `FOR UPDATE SKIP LOCKED LIMIT 1`.
2. Per-league lock: a partial unique index `unique(league_id) WHERE status='running'`. A queued job
   for a league that already has a running row is skipped by the candidate query (`NOT EXISTS` a
   running row with an unexpired lease). An expired running row is **reclaimed in place** (same row,
   `UPDATE`), so the index never blocks recovery and never admits two running rows.
3. Provider cap: lock a free slot `provider_slots WHERE provider=? AND (job_id IS NULL OR
   lease_expires_at < now()) FOR UPDATE SKIP LOCKED LIMIT 1`; no free slot → release and stop. Row
   locks make the cap race-safe under READ COMMITTED (counting running rows would not be).
4. Fence: `UPDATE leagues SET sync_fence = sync_fence + 1 ... RETURNING sync_fence` → stored as the job's
   `lease_token`; job `status='running'`, `lease_expires_at = now() + 2 min`, `attempt + 1`.

### 3.4 Heartbeat and superseded workers
- Every 30 s the worker runs `UPDATE sync_jobs SET lease_expires_at = now() + 2 min, heartbeat_at = now()
  WHERE id=? AND lease_token=?` (also extends its slot). Zero rows → lease lost → abort without
  committing.
- Fencing at commit: each league database has `_sync_meta(fence BIGINT, data_version BIGINT,
  fi_global_version VARCHAR, committed_at TIMESTAMP)`. The commit transaction first runs
  `UPDATE _sync_meta SET fence = ? ... WHERE fence < ?`; if no row changed, it rolls back. A superseded
  worker can therefore never overwrite data committed by a newer one, even after a pause past its lease.
  The Postgres lease check before commit is best-effort; the DuckDB fence is the guarantee.

### 3.5 Atomic updates
- Fetch everything from the provider into memory (no writes) → compute derived tables (waivers,
  managers, ROS projections, Ours re-scored to league scoring) reading `fi_global` inside one snapshot
  and recording its version → write all tables plus `_sync_meta` in **one DuckDB transaction** on
  `lg_<id>`. Any failure leaves the previous data intact.
- The existing ESPN sync writes incrementally; it is restructured into fetch → compute → commit
  (Phase 3) without changing parsing or engine logic.
- Time budget: a job must fit one function's `maxDuration`; Phase 0 measures real durations. If it
  does not fit, the fetch stage stores raw provider JSON in Postgres and a second job computes and
  commits.

### 3.6 Failure classification
| Signal | Handling |
|---|---|
| Network error, 5xx, 429, MotherDuck transient | Retry with backoff + jitter: 1 m, 5 m, 20 m, 1 h, 3 h → `dead`. A 429 also pauses the provider (Retry-After) |
| Authenticated 401/403 on a private league | Ambiguous until confirmed. Credential → `suspect`, retry. **Confirmed invalid** only when ≥3 authenticated attempts over ≥6 h all fail with 401/403 *and* a control request in the same run succeeds (ESPN reachable, e.g. a public league endpoint). Then `invalid`, league `stale`, owner notified. Never auto-delete the credential or data |
| 404 league not found | Same confirmation rule (≥3 over ≥6 h, control succeeds) → league `unavailable`. Data kept; normal retention only |
| Unauthenticated probe says private | Restrict immediately (safe direction); no data deleted |
| Parse/schema error | `dead` immediately, alert; the previous data stays served |

### 3.7 Status reflects what readers see
- Readers report the `_sync_meta` they actually read (`data_version`, `committed_at`,
  `fi_global_version`) with every response; the "data as of" label comes from that, not from Postgres.
- Postgres `leagues.last_committed_version` is only used to show "a newer sync is propagating" when it
  is ahead of what the reader sees (read-replica lag).
- When `fi_global` advances, a `recompute` job per active league rebuilds the derived tables from stored
  league data (no provider fetch). Until it lands, responses carry both versions.

## 4. Phase 0: MotherDuck validation spike (go/no-go)

Using the API's real token type (`MOTHERDUCK_API_TOKEN`, a standard read/write token), from the laptop
and from a real Vercel Function in the production region, against 60 `spike_lg_*` databases (copies
of the Los Subies league tables plus the global tables in `spike_fi_global`):

| Area | Test | Pass criterion |
|---|---|---|
| Isolation | SQL attempts from a connection opened as `md:spike_lg_001?attach_mode=single`: `SHOW DATABASES`, `ATTACH 'md:spike_lg_002'`, fully qualified `SELECT` from another league, `USE`, `CREATE DATABASE`, `DROP`, writes; with and without `SET enable_external_access=false` + `SET lock_configuration=true` | Documented truthfully. Expected: no credential-level boundary; whether config locking blocks `ATTACH` decides whether we get a defense-in-depth guard |
| Read-only | Which mechanism blocks writes for the API: read-scaling token (plan?), read-only attach of shares, `access_mode`, separate reader identity | At least one affordable mechanism, or the gap is reported for an owner decision |
| `fi_global` attach | Attach global read-only next to a league DB; `search_path` resolves unqualified names; writes to global fail | Works |
| Latency | Connect + attach + first query, cold and warm, p50/p95 from Vercel | p95 cold < 1.5 s, warm added < 300 ms |
| Concurrency | 5 parallel writers to different league DBs + readers; 2 writers to one DB | Different DBs independent; same-DB conflict behaviour documented; fence prevents stale overwrite |
| Atomicity | Multi-table write transaction killed mid-way | Old data intact |
| Snapshot | One `BEGIN` across league DB + `fi_global` while both are rewritten | Consistent snapshot across both, or the gap documented |
| Replica lag | Commit from writer connection, poll from a separate reader connection/process | Lag measured; status rule in 3.7 holds |
| Sync duration | Full Los Subies ESPN fetch + derived compute | Fits Vercel Pro `maxDuration`, or the split in 3.5 is required |
| Cost/limits | Storage per league, compute per sync and per page view, database-count behaviour at 60, plan pricing | Monthly projection at 100 and 1,000 leagues |
| Migrations | Apply a schema change to 60 DBs | Time recorded |
| Vercel Queues | Publish/consume from Python on this team | Available or fallback chosen |

Go = isolation story acceptable (server-enforced, with whatever guard exists), an affordable read-only
mechanism exists, latency targets met, atomic writes and fencing hold, cost acceptable. Otherwise the
spike report goes to Havyn with the numbers and B is revisited against A/C.

## 5. Testing
- **Los Subies golden tests:** before any refactor, record every existing GET endpoint's JSON against a
  frozen database copy and fixed clock; after each phase, legacy URLs and `/v2/leagues/{lg}/…` must
  match exactly (volatile timestamps masked). CI runs this on the E2E fixture league; it also runs
  locally against the real Los Subies database before every phase merges.
- **Cross-league leak tests:** two fixture leagues with colliding player and team ids and different
  scoring; every endpoint's output for A is identical with or without B; interleaved A/B requests never
  share a cache entry; a request for B by an A-only user is 404.
- **Authorization matrix:** {anonymous, viewer, admin, non-member} × {public, private, stale,
  visibility-expired, flipped-private} × every league route, enumerated from the router so a new route
  without the guard fails CI.
- **Credentials:** AAD swap fails; rewrap round-trips; URL allowlist; secrets absent from logs, errors,
  reprs.
- **Sync:** lease reclaim in place, heartbeat loss aborts, fence rejects a stale commit, slot cap under
  concurrent claimers, duplicate enqueue collapse, failure classification (ambiguous 403 never
  invalidates), failure injected mid-commit leaves data intact.
- **Postgres:** RLS per role, `SET LOCAL ROLE` + claims isolation across pooled connections,
  SECURITY DEFINER functions reject other users.
- Existing suite, E2E and the league-scope ratchet stay green throughout.

## 6. UX and rollout
- URLs `/l/<lg_id>/<page>`; old URLs redirect to the viewer's last-viewed league (Los Subies for Havyn).
- Public league visitors pick any roster as their perspective without signing in; signing in saves it
  and enables pins and private connections. Authentication never gates public analytics.
- Owner-only research pages (projection performance, research, Ours promotion) remain platform-admin
  only.
- Phases, acceptance criteria and rollback: see the plan.

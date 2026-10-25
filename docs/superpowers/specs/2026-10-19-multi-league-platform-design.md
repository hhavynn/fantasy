# Multi-league platform: design

- Status: approved design (2026-10-19), **revised 2026-10-20 for free tiers**: DuckDB files in Cloudflare R2,
  GitHub Actions sync, Supabase free, no MotherDuck. The paid-tier Option B (database per league in
  MotherDuck) is retired; its spike findings that still apply are folded in below.
- Supersedes for the hosted product: parts of [ADR-0001](../../multileague/ADR-0001-league-context-and-tenancy.md)
  (custom user assertion, "never collect ESPN cookies") and the [roadmap](../../multileague/roadmap.md) phases 2–4.
  An ADR-0002 recording these changes lands with Phase 1.
- Plan: [2026-10-19-multi-league-platform.md](../plans/2026-10-19-multi-league-platform.md)

## Goal

Make the existing fantasy website usable by anyone for their own league, not only for Los Subies,
without rebuilding features that already work, **while staying on free tiers**. Anyone can view a public
league; signed-in users can save preferences and connect private ESPN leagues; Sleeper leagues work too;
every page reaches parity before sign-ups open.

## Decisions

| Topic | Decision |
|---|---|
| Cost | Free tiers only. Any paid requirement is raised with a free alternative before building on it |
| Audience | Anyone on the internet (non-commercial: no ads or payments, per Vercel Hobby terms; donations allowed) |
| ESPN access | Public leagues by default (no cookies); opt-in private leagues with user-pasted `espn_s2`/`SWID`, encrypted |
| Other providers | Sleeper (public API). Yahoo out of scope |
| Accounts / control plane | Supabase free (Auth + Postgres with RLS), project `fantasy-intel` (us-east-1) |
| League data | **One immutable DuckDB file per league version in Cloudflare R2**, plus one global file |
| Read path | FastAPI on Vercel (Hobby) downloads the authorized league's current file to `/tmp`, opens it read-only |
| Sync host | **GitHub Actions** (public repo: standard runners are free); Postgres queue stays authoritative |
| Launch scope | Full parity on every page before sign-ups open |
| Global data | Laptop publishes `global.duckdb` to R2 for now; GitHub Actions takes over in Phase 7 |
| UX | Preserve the current site; add sign-in, a league switcher, a perspective picker and a Connect flow |

## Free-tier budget

| Service (free tier) | Limit that matters | How the design stays inside it |
|---|---|---|
| Cloudflare R2 | 10 GB-month storage, 1M writes, 10M reads/month, **egress free** | Full-file downloads cost nothing in transfer; old versions garbage-collected (keep 3 per league) |
| Vercel Hobby | 4 h Active CPU, 1M invocations, 360 GB-h memory/month; cron ≥ 1/day; 300 s max duration; `/tmp` 500 MB | Only serving runs on Vercel; queries are local and fast; sync never runs on Vercel |
| GitHub Actions | Free for public repos; scheduled workflows ≥ 5 min, best effort; disabled after 60 days without repo activity; **logs are public** | Drain workflow every 15 min + `repository_dispatch`; jobs print nothing about league data |
| Supabase free | 500 MB DB, 50k MAU, 5 GB egress, 2 active projects/org, pauses after 1 week idle | Control plane only (small rows); scheduled drains keep it active |
| MotherDuck | Not used | Removed from the hosted path (Phase 1a); Lite's 10 CU-h cap was below today's traffic |

Measured in Phase 0b: Vercel Blob (10 GB transfer, 2,000 uploads/month, 30-day lockout when exceeded) and
Supabase Storage (50 MB max file, 5 GB egress) cannot carry full-file downloads; R2 is the only free
store that can. Range reads (DuckDB `ATTACH` over HTTP) fetched ~100% of the file anyway, so they save no
transfer.

## 1. Architecture

```
Browser ── Next.js (Vercel Hobby) ── Supabase Auth (@supabase/ssr session cookie)
              │  forwards Authorization: Bearer <Supabase access token> (when signed in) + X-API-Key
              ▼
         FastAPI (Vercel Hobby) ── verifies the Supabase JWT itself (JWKS, cached)
              │                 ── Supabase Postgres: authorization + current snapshot pointer per league
              ▼  (R2 read-only token)
         Cloudflare R2 (private bucket)
           global/<version>.duckdb          ← laptop publishes (Phase 7: GitHub Actions)
           leagues/<lg_id>/<version>.duckdb ← written only by sync jobs (R2 read/write token)
              ▲
         GitHub Actions sync workers ── Supabase Postgres (fi_sync role: queue, leases, pointer swap)
```

- **Auth.** FastAPI verifies Supabase access tokens directly: signature against the project's JWKS
  (`/auth/v1/.well-known/jwks.json`, cached, refreshed on unknown `kid`), `iss`, `aud=authenticated`,
  `exp`; the user id is `sub`. The Phase 1 HMAC user assertion is removed. `FANTASY_API_KEY` remains
  service authentication (only our Next.js server calls the API). No token = anonymous.
- **Authorization** is decided in FastAPI from Postgres for every request, before any league file is
  touched (Section 2). The same query returns the league's current snapshot pointer. RLS is a second
  line of defense, not the only check.
- **Read path.** The pointer names an immutable object `leagues/<lg_id>/<version>.duckdb`. Each Vercel
  instance keeps a byte-bounded LRU of files in `/tmp` (≤ 350 MB total; download to a temp name, then
  atomic rename; one download per object per instance behind a lock). The request opens the league
  file `read_only`, attaches the current global file read-only, sets `search_path = league, global`,
  and runs the existing engines with the request's *perspective team* instead of
  `fantasy_teams.is_mine`. A version changes only when a new object is published, so cached files
  never go stale; responses carry the version they were served from.
- **Credential-level read-only.** The API holds an R2 token scoped to **Object Read** on the bucket; only
  sync workers and the global publisher hold write tokens. Cross-league isolation is server-enforced:
  object keys are derived only from an authorized league id and its pointer, no user-supplied SQL or
  paths exist, and the leak tests in Section 5 cover every route.
- **UX.** Same pages, components and engines. Additions: sign-in/account menu, league switcher,
  perspective picker, Connect flow. Current URLs keep working (redirect to the viewer's default league).

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
  dormant → league objects deleted; re-import rebuilds it from the provider.
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
  in-app warning at day 30. Admin delete = 7-day soft delete, then the league's objects are deleted.
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
- Envelope encryption with `cryptography`: a random 256-bit data key per credential encrypts
  `{"espn_s2","swid"}` with AES-256-GCM (96-bit random nonce; AAD = `"fi-cred-v1" ‖ league_id ‖
  credential_id ‖ owner_user_id`, so a ciphertext moved to another row fails to decrypt). The AAD holds
  only values that never change for the life of the ciphertext; it deliberately excludes the wrapping-key
  version.
- The data key is wrapped with **RSA-OAEP-3072 (SHA-256)** under a public key `CREDENTIALS_PUBKEY_v{n}`
  held by the API. The private key `CREDENTIALS_PRIVKEY_v{n}` exists only as a GitHub Actions secret,
  so the internet-facing API can store credentials but never decrypt them. Columns: `ciphertext`, `nonce`,
  `aad_format` (immutable, `"fi-cred-v1"`), `wrapped_data_key`, `wrap_key_version` (mutable).
- Rotation: add keypair `v{n+1}`; a `rewrap` job in Actions unwraps each data key with the private key
  named by its row's `wrap_key_version`, rewraps it under `v{n+1}`, and updates `wrapped_data_key` and
  `wrap_key_version` in one statement. The cookie ciphertext, nonce and AAD are untouched, so they still
  authenticate. Once no row references `v{n}`, its private key is deleted.
- ESPN cookies are account-wide session cookies. They are used only for GETs to ESPN's fantasy read
  endpoint for that league and season (URL allowlist enforced in the sync client); never for writes,
  other leagues or other ESPN endpoints; never logged, returned or included in errors (redaction
  tests; `::add-mask::` in Actions). Validated by one fetch at submission (the API does this before
  encrypting). Connecting requires explicit consent text: they grant access to the user's ESPN
  account; signing out of ESPN everywhere revokes them.

### Postgres roles (least privilege, no `BYPASSRLS`)
- `fi_api` (request path): `NOINHERIT`, member of `authenticated` and `anon`. Each request transaction
  runs `SET LOCAL ROLE authenticated|anon` and `set_config('request.jwt.claims', <verified claims>,
  true)`, so RLS and `auth.uid()` apply. No grants on `league_credentials`. Mutations that need more
  than RLS allows (redeem invite, store credential, transfer admin) go through `SECURITY DEFINER`
  functions that check `auth.uid()` themselves, with `search_path` pinned.
- `fi_sync` (sync workers in GitHub Actions): no `BYPASSRLS`. Table grants limited to `leagues`, `sync_jobs`,
  `provider_slots`, `league_snapshots`, `league_snapshot_history`; RLS policies `TO fi_sync` on those tables. Credentials are read
  only through `SECURITY DEFINER sync_credential(job_id, lease_token)`, which returns ciphertext only for
  a league whose job is currently leased with that token.
- The Supabase service-role key is never deployed to Vercel or GitHub Actions.
- Transaction-local claims are verified not to leak across pooled connections (Supavisor transaction
  mode) in Phase 1 tests.

## 3. Sync jobs

Postgres is the authoritative queue. Workers run in GitHub Actions. No other infrastructure.

### 3.1 Tables
- `sync_jobs(id, league_id, kind ∈ {initial, scheduled, manual, probe, recompute, rewrap}, status ∈
  {queued, running, succeeded, failed, dead}, attempt, max_attempts=5, run_after, lease_owner,
  lease_token, lease_expires_at, heartbeat_at, provider_slot, idempotency_key, error_code,
  error_detail (redacted), global_version, created_at, started_at, finished_at)`.
- `leagues.sync_fence BIGINT` — monotonic, incremented on every claim or reclaim of a job for that league.
- `league_snapshots(league_id PK, object_key, data_version, fence, global_version, committed_at)` — the
  current pointer. A league has no row until its first publish; until then the API answers the league's
  routes with a "first sync in progress" state (from `sync_jobs`), never with another league's data. `league_snapshot_history` keeps the last 3 for rollback and garbage collection.
- `provider_slots(provider, slot_no, job_id NULL, lease_expires_at)` — N rows per provider (ESPN 2,
  Sleeper 4).

### 3.2 Dispatch
- Connecting a league inserts the `initial` job and commits, then calls GitHub's `repository_dispatch`
  (fine-grained token limited to this repo's dispatch permission, held by the API) and awaits the HTTP
  response. A failed dispatch is logged; the job row still exists.
- Backstop: a scheduled workflow every 15 minutes runs the drain (GitHub schedules are best effort and
  can be delayed; the UI shows "queued" until a worker claims the job). The drain also enqueues due
  scheduled refreshes (staggered per league, more often on game days, dormant leagues skipped).
- A workflow `concurrency` group bounds parallel drains; each run claims jobs until the queue is empty
  or its time budget (20 min) is used.

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
  WHERE id=? AND lease_token=?` (also extends its slot). Zero rows → lease lost → abort without publishing.
- **Fenced publish:** the only step that makes data visible is an upsert whose update arm keeps the fence
  check, so the first publish of a new league inserts the pointer and later publishes replace it only
  when newer:
  ```sql
  INSERT INTO league_snapshots (league_id, object_key, data_version, fence, global_version, committed_at)
  VALUES (?, ?, ?, ?, ?, now())
  ON CONFLICT (league_id) DO UPDATE
    SET object_key = EXCLUDED.object_key, data_version = EXCLUDED.data_version, fence = EXCLUDED.fence,
        global_version = EXCLUDED.global_version, committed_at = EXCLUDED.committed_at
    WHERE league_snapshots.fence < EXCLUDED.fence
  RETURNING league_id;
  ```
  No row returned → a newer worker already published; this worker's object is orphaned and
  garbage-collected. A superseded worker can therefore never replace newer data, even after a pause past
  its lease, and two first publishes racing on a new league resolve the same way.

### 3.5 Atomic updates
- Fetch everything from the provider into memory (no writes) → build a complete league DuckDB file on
  the runner (parsing, waivers, managers, ROS projections, Ours re-scored to league scoring, reading the
  current global file and recording its version) → upload it as a new immutable object → fenced pointer
  swap. Readers see the old file or the new file, never a mix. Any failure before the swap changes
  nothing visible.
- The existing ESPN sync already fetches before writing (`fetch_all` → `write_all`); the worker runs it
  against a fresh local file instead of the shared database, without changing parsing or engine logic.
- Measured: a full Los Subies sync takes ~46 s on a laptop (fetch 9 s, write 1.5 s, analytics 31 s).

### 3.6 Failure classification
| Signal | Handling |
|---|---|
| Network error, 5xx, 429, R2/Supabase transient | Retry with backoff + jitter: 1 m, 5 m, 20 m, 1 h, 3 h → `dead`. A 429 also pauses the provider (Retry-After) |
| Authenticated 401/403 on a private league | Ambiguous until confirmed. Credential → `suspect`, retry. **Confirmed invalid** only when ≥3 authenticated attempts over ≥6 h all fail with 401/403 *and* a control request in the same run succeeds (ESPN reachable, e.g. a public league endpoint). Then `invalid`, league `stale`, owner notified. Never auto-delete the credential or data |
| 404 league not found | Same confirmation rule (≥3 over ≥6 h, control succeeds) → league `unavailable`. Data kept; normal retention only |
| Unauthenticated probe says private | Restrict immediately (safe direction); no data deleted |
| Parse/schema error | `dead` immediately, alert; the previous data stays served |

### 3.7 Status reflects what readers see
- Every response carries the `data_version` and `global_version` of the files it actually read; the
  "data as of" label comes from that.
- When the global file advances, a `recompute` job per active league rebuilds the league file from its
  stored raw provider data (no provider fetch). Until it lands, responses carry both versions.

### 3.8 Public logs and secrets in GitHub Actions
- Workflows run only on `schedule`, `repository_dispatch` and `workflow_dispatch` — never on
  `pull_request` from forks — so secrets (Postgres `fi_sync` URL, R2 write token, `CREDENTIALS_KEK_v{n}`,
  GitHub dispatch is inbound only) are never exposed to forked code.
- Logs are public: workers print only job ids, durations and error codes. Decrypted cookies are
  registered with `::add-mask::` before use; exceptions are redacted before printing; no league data,
  names or team names are logged.

## 4. Spikes

- **Phase 0 (MotherDuck, retired):** [`spikes/motherduck/REPORT.md`](../../../fantasy-intel/spikes/motherduck/REPORT.md).
  Findings that still apply: multi-table writes are atomic and fencing works; a per-instance lock is
  needed around shared per-instance resources (concurrent requests raced on `ATTACH`); a full sync
  takes ~46 s. MotherDuck itself is dropped: Lite caps compute at 10 CU-hours/month with a 1 CU-second
  minimum per query, below today's ~24 CU-hours/month of API traffic.
- **Phase 0b (object storage):** [`spikes/storage/REPORT.md`](../../../fantasy-intel/spikes/storage/REPORT.md).
  Go criterion: a free store that carries full-file downloads from `iad1` with p95 < 2 s for the
  current 95 MB snapshot and no transfer charge. **Result: R2**, with parallel ranged downloads (one
  stream: p95 4.0 s; 4–16 parts: p95 ~1.0–1.4 s). Vercel Blob and Supabase Storage fail on free-tier
  transfer/file-size limits.

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
- **Credentials:** AAD swap fails; rewrap round-trips and the cookie ciphertext still decrypts under the new
  key with the AAD unchanged; a row is never decrypted with a key other than its `wrap_key_version`; URL allowlist; secrets absent from logs, errors,
  reprs.
- **Sync:** lease reclaim in place, heartbeat loss aborts, fence rejects a stale commit, the first publish of a
  new league inserts its pointer (and racing first publishes keep the higher fence), slot cap under
  concurrent claimers, duplicate enqueue collapse, failure classification (ambiguous 403 never
  invalidates), failure injected mid-commit leaves data intact.
- **Postgres:** RLS per role, `SET LOCAL ROLE` + claims isolation across pooled connections,
  SECURITY DEFINER functions reject other users.
- Existing suite, E2E and the league-scope ratchet stay green throughout.

## 6. UX and rollout
- URLs `/l/<lg_id>/<page>`; old URLs redirect to the viewer's last-viewed league (Los Subies for Havyn).
- Public league visitors pick any roster as their perspective without signing in; signing in saves it
  and enables pins and private connections. Authentication never gates public analytics.
- Owner-only research pages (projection performance, Ours promotion) remain platform-admin only. The research
  lab, game splits and pace lab became league-member pages (#289 research-tools rulings): authorized like every
  other league page, with no betting lines in game splits until the owner decides.
- Phases, acceptance criteria and rollback: see the plan.

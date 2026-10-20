# Phase 0 report: MotherDuck database-per-league spike

Run 2026-10-19/20 (PDT). duckdb 1.5.6. 60 league databases (`spike_lg_001..060`, clones of the Los Subies
league tables) + `spike_fi_global`. Reads used `MOTHERDUCK_API_TOKEN` (the hosted API's token type: a
standard read/write token); writes used the laptop token. Raw results: `results/*.json`.

## Verdict: conditional GO for Option B

The data plane works: isolation between leagues is clean when the server picks the database, writes are
atomic, fencing works, snapshots are consistent, replica lag is negligible, latency meets targets on warm
instances, and syncs fit easily. Two items must close before Phase 1 starts:

1. **Reader identity (blocker).** No mechanism inside one MotherDuck identity stops a token from writing
   or dropping databases. The fix is a separate **service account** that owns nothing and reads per-league
   **restricted shares**. Lite allows 2 service accounts. Creating it is an account action for Havyn; then
   one experiment proves it can read shares but cannot write or `DROP` owner databases.
2. **Cost/plan decision.** Per-query billing on Pulse makes query count the cost driver (numbers below).
   Response caching keyed by `data_version` is required before onboarding strangers.

## Results against the acceptance list

| Criterion | Result | Pass |
|---|---|---|
| Isolation documented from real SQL | See "Isolation" — `attach_mode=single` hides other DBs only until `ATTACH`; the API token type can then read, write, `CREATE` and `DROP` any account database | documented |
| Server-enforced isolation design | Required (no credential boundary exists inside one identity) | yes, by design |
| Read-only mechanism | Shares are read-only even through a read/write connection; `access_mode=read_only` blocks DML but **not** `CREATE/DROP DATABASE`; a reader service account + restricted shares is the candidate | **pending service account** |
| `fi_global` read-only beside league + `search_path` | Works; global writes rejected | yes |
| Warm league switch (Vercel iad1) | total p50 84 ms, p95 169 ms (attach p95 88 ms, query ~81 ms) | yes (< 300 ms) |
| Fresh connection in a warm instance (Vercel) | p50 981 ms, p95 1,073 ms | yes (< 1.5 s) |
| Brand-new function instance, first request | ~2.1–2.4 s (MotherDuck extension download + auth); same cost production pays today | no (mitigation below) |
| Atomic multi-table write under kill | Writer `os._exit` mid-transaction: all 5 tables + `data_version` unchanged | yes |
| Fence rejects stale commit | B(6) commits, stale A(5) updates 0 rows and rolls back, C(7) commits; final fence 7 | yes |
| Cross-DB snapshot | Reader transaction over league + global kept `(0, v1)` while a writer committed both; after the transaction `(1, v2)` | yes |
| Reader lag (separate process, API token) | p50 50 ms, p95 55 ms, max 56 ms (≈ one round trip) | yes |
| Concurrent writers, different DBs | 5 writers × 10 five-table rewrites, ~1 s each, 0 errors; 5 readers, ~1,040 reads, 0 torn | yes |
| Concurrent writers, same DB | optimistic conflicts ("Conflict on tuple deletion"); loser aborts cleanly, no corruption | yes (lock required, as designed) |
| Sync duration (Los Subies, laptop) | fetch 9.1 s, write 1.5 s, analytics 30.9 s, commit to MotherDuck 4.7 s; total 46 s | yes (fits Pro `maxDuration`) |
| Database count | 60 created (59 clones, p50 265 ms each), no limit hit | yes |
| Migration fan-out (60 DBs) | 5.1 s sequential, 0.7 s with 8 threads, 0 errors | yes |
| Vercel Queues for Python | Documented Python SDK (`vercel.queue.subscribe`, at-least-once); smoke test is Phase 3 task 1 | yes (docs) |
| Cost projection | Below | needs decision |

## Isolation (API token type, `md:spike_lg_001?attach_mode=single&saas_mode=true`)

| Attempt | Result |
|---|---|
| `duckdb_databases()` | only own league, `md_information_schema`, system |
| `SELECT … FROM spike_lg_002.main.fantasy_teams` (no attach) | Binder error: catalog does not exist |
| `ATTACH 'md:spike_lg_002'` then select | **succeeds** (12 rows) |
| `INSERT/DELETE` into the other league | **succeeds** |
| `CREATE DATABASE` / `DROP DATABASE` | **succeed** |
| same, with `access_mode=read_only` | DML rejected; **`DROP DATABASE spike_lg_059` (existing) succeeded** |
| `SET enable_external_access=false; SET lock_configuration=true` (without `saas_mode`) | blocks local files (`read_csv('/etc/hosts')` denied) but **not** `ATTACH 'md:…'` or cross-DB writes |
| with `saas_mode=true` | configuration is already locked, so the guard cannot be added on top |
| share of league 001 attached by URL | reads work; `INSERT` rejected even on a read/write connection; same connection still dropped `spike_lg_058` |

**Side finding about production today:** the hosted API's token type can `DROP DATABASE fantasy_intel`
through the same path. Practical risk is low (the API runs only our own SELECTs), but the reader service
account would close it for the current site as well.

## Design amendments from the spike

1. **Reader identity:** API reads use a service account that owns no databases and is granted READ on
   one restricted share per league plus `fi_global`. Sync jobs keep a separate writer token. Share
   creation and grant become part of league registration.
2. **Attach lock:** concurrent requests in one Vercel instance raced on `ATTACH IF NOT EXISTS` ("database
   … already exists", 4 of 20 in a 10-way burst). Attaches go behind a per-instance lock, and "already
   exists" counts as success. Attached leagues are LRU-detached to bound memory.
3. **Orphaned transactions:** a killed writer's transaction blocked other writers to the same rows for
   ~20–30 s. Sync classifies `Conflict on update/tuple deletion` as transient (retry with backoff ≥ 30 s);
   reclaim after a crash expects it.
4. **One database per write transaction:** a transaction cannot write two databases. Already true of the
   design (league commits and `fi_global` publishes are separate jobs); now stated explicitly.
5. **Cold instances:** bundle the MotherDuck extension in the function (no download on cold start) and
   rely on Fluid instance reuse; re-measure in Phase 2. Today's production has the same cold path.
6. **Response cache:** cache league API responses per instance keyed by `(league, data_version,
   visibility_epoch, perspective, route, params)`, always after authorization. League data changes only
   when a sync commits, so most repeat views cost zero MotherDuck queries.

## Cost

Pricing (MotherDuck docs, us-east-1): Lite $0/month with 10 CU-hours and 10 GB included, 2 service
accounts, Pulse only at **$0.60/CU-hour, metered per query with a 1 CU-second minimum**. Business
$250/month + usage; Standard duckling $2.40/hour wall-clock with ≥ 60 s cooldown; read scaling is
Business-only. The account currently runs **Standard** ducklings (query history), consistent with the
7-day free trial (database history starts 2026-10-15), so it falls back to Lite/Pulse around 2026-10-22.

Measured production (Havyn alone, last 7 days): 23,329 API queries, average 4.3 ms, ~10 queries per API
connection. On Pulse that is ≥ 23,329 CU-s/week ≈ **28 CU-hours/month — already over Lite's 10** (≈ $11/month
overage at today's traffic).

Per-league model (Pulse, no response cache): ~12 members, ~90 page views/league/week, ~30 queries/page
view → ≈ 3.3 CU-h/month reads; ~6 syncs/day at ~10 CU-s each → ≈ 0.5 CU-h/month; storage ≤ 16 MiB.

| Leagues | Pulse, no cache | Pulse, ~80% cache hits | Business + always-on Standard |
|---|---|---|---|
| 100 | ≈ $230/month | ≈ $55/month | ≈ $2,000/month |
| 1,000 | ≈ $2,300/month | ≈ $500/month | ≈ $2,000/month (one duckling may not keep up) |

Storage is negligible ($0.04/GB-month; 1,000 leagues ≈ 16 GB). Vercel sync compute (≈ 60 s/sync) is
separate and not measured here; it scales with sync frequency, so syncs should run only when a league
is active.

## Open items for Havyn

- Create one MotherDuck **service account** (Settings → Service accounts) and add its token to
  `fantasy-intel/.env` as `MOTHERDUCK_READER_TOKEN`; the spike then proves read-only/no-drop via shares.
- Choose the plan direction: stay on Lite/Pulse with response caching (cheapest at this scale), or plan
  for Business later.
- Teardown: `uv run python ../spikes/motherduck/teardown.py` (from `backend/`) drops all `spike_*`
  databases and the spike share; the `fi-md-spike` Vercel project can then be deleted. Keeping both until
  the service-account test finishes is cheaper than rebuilding them.

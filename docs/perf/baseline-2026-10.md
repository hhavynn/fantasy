# Performance baseline and free-tier budgets (2026-10, issue #201)

What a page waits on, cold and warm, for Overview, Waivers, Trades (and the Discover trade scan), Matchup and Players;
how that holds up with 1, 10 and 25 leagues loading at once; and what the issue #201 changes cost against the free tiers.

## Method

- Script: `fantasy-intel/scripts/perf_baseline.py`. Everything is local and synthetic: a throwaway control-plane
  database (`tests/pg_harness.py`), the E2E fixture league published as N public leagues to a local object directory
  by the real publisher, and the real API app called in-process (httpx `ASGITransport`: one event loop and its thread
  pool, like one API instance). Production, R2, Supabase and Vercel are not touched.
- The object store sits behind a simulated R2 link: 50 ms per object plus 30 MB/s (the single-stream rate `r2.py`
  documents for Vercel iad1). The fixture's files are 16.5 MB (league) and 4.5 MB (global).
- A page is the backend calls its server render makes, in the order it makes them: the root layout's `/api/status` and
  the league profile first, then the page's own calls in parallel (Overview: overview, matchup, start-sit, top waivers;
  Waivers: the list and the band; Trades: options; Discover: context, then the multi-week summary scan; Matchup:
  matchup; Players: the list). Page time is how long until the last call of the last wave has answered. The Next.js
  render on top is not included.
- **Cold:** a fresh instance (no files, no answers). **Warm:** files cached, every answer computed again (response cache
  and trade-search memo emptied). **Cached:** answers from the per-instance response cache. Single-page rows are the
  median of 3 fresh instances.
- Machine: Linux x86_64, Python 3.12.3, 4 CPUs **shared with other agents (load average 24-27 during both runs)**.
  Absolute numbers are noisy, so compare the two columns and rows, not single figures.

Rerun:

```sh
cd fantasy-intel/backend
FI_TEST_PG_DSN=postgresql://postgres@127.0.0.1:55432/postgres \
  uv run python ../scripts/perf_baseline.py --leagues 1,10,25 --repeat 3 [--r2-latency-ms 50 --r2-mbps 30] [--json out.json]
```

It takes about 4 minutes on this machine. Leave out `FI_TEST_PG_DSN` to use a local `initdb`.

## One league, per page (ms)

| Page | Cold, before | Cold, after | Warm, before | Warm, after | Cached, before | Cached, after | Budget cold / warm / cached |
|---|---:|---:|---:|---:|---:|---:|---|
| Overview | 1371 | 1491 | 355 | 254 | 84 | 33 | 4000 / 1500 / 150 |
| Waivers | 1098 | 1250 | 170 | 293 | 36 | 37 | 4000 / 1500 / 150 |
| Trades | 1094 | 1049 | 168 | 180 | 23 | 25 | 4000 / 1500 / 150 |
| Trades → Discover (multi-week scan) | 3667 | 3854 | 2438 | 2757 | 64 | 27 | 10000 / 8000 / 150 |
| Matchup | 1170 | 1129 | 150 | 108 | 29 | 38 | 4000 / 1500 / 150 |
| Players | 1284 | 1013 | 81 | 77 | 17 | 17 | 4000 / 1500 / 150 |

"Before" is origin/main (5315c96) and "after" is this branch, on the same script and machine. Every page is within budget
in both runs. Single-league differences are within the noise. Each cold page costs 2 R2 GETs (league + global file).
About 0.8 s of each cold figure is the simulated download.

## Leagues loading at once (one instance)

| Leagues | Overview cold p50 / p95 | Overview warm p50 / p95 | Cached p50 / p95 | Discover scan, warm p50 / max | R2 GETs | Failed calls |
|---:|---|---|---|---|---:|---:|
| 1, before | 1203 / 1203 | 284 / 284 | 77 / 77 | 2633 / 2633 | 2 | 0 |
| 1, after | 1657 / 1657 | 265 / 265 | 37 / 37 | 2347 / 2347 | 2 | 0 |
| 10, before | 5312 / 5419 | 2306 / 2663 | 307 / 337 | 26799 / 26799 | 11 | 0 |
| 10, after | 4964 / 5075 | 3285 / 3291 | 574 / 623 | 26211 / 26944 | 11 | 0 |
| 25, before | 8665 / 11802 | 7058 / 11649 | 2307 / 3096 | 57282 / 59503 | 81 | **30** (503 `CacheFull`) |
| 25, after | 9938 / 13054 | 8196 / 11190 | 2660 / 3409 | 56405 / 60871 | 118 | **4** (503 `CacheFull`) |

**Stampede** (10 viewers open one league's Overview at once, cold): before, p50 3399 ms, every viewer computed every
answer. After, p50 1653 ms: each of the 6 answers was computed once and 52 answers were shared from the in-flight
identical request. Both runs made 2 R2 GETs, because the file download was already single-flight.

What the numbers say:

- **25 cold leagues do not fit one instance's cache.** 25 × 16.5 MB = 413 MB, over the 350 MB `/tmp` bound. Before this
  branch, a load that found every file pinned by running requests got an immediate 503. Now it waits up to 8 s for a
  request to release its files, so failures drop from 30 to 4. The cost is extra re-downloads (81 to 118 R2 GETs),
  because the working set is larger than the cache. Real Vercel traffic spreads across instances, so 25 different
  leagues arriving at the same moment on one instance is a worst case.
- **The Discover scan is CPU-bound.** Ten scans at once take about 26 s each on one instance, and 25 take about 57 s.
  The page streams the scan under its header (`app/trades/discover`), so the page shows something at once. A burst
  of 25 is still close to the function time limit. This is the largest remaining risk (see below).
- Overview, Waivers, Trades, Matchup and Players stay in budget for one league. With 10 leagues cold at once they
  are over the cold budget (about 5 s, mostly thread-pool queueing on 4 busy CPUs).

## Guardrails added (issue #201)

| Guardrail | Where | Effect |
|---|---|---|
| `Server-Timing` on every API response (`auth`, `files` cold/warm/stale, `wait`, `cache` hit, `app` total + size bucket) | `app/monitor/timing.py`, `league_scope.py` | Shows where a slow page spends its time. No league id, name or query string. |
| Opt-in perf log: `FI_PERF_SAMPLE` (fraction), `FI_PERF_SLOW_MS` (threshold); off by default | `app/monitor/timing.py` | One line per sampled request with the route template, status, ms and phases. Log volume is unchanged unless turned on. |
| `API_TIMING` (`1` or a fraction) on the Next.js server; off by default | `frontend/lib/api-timing.ts`, `lib/api.ts` | One line per sampled backend call: method, path template (`/v2/leagues/{league}/api/players/{id}`), status, ms, and the backend's own total. |
| Response single flight | `league_scope.py` (`ResponseCache.begin/land`) | Identical concurrent GETs (same league, versions, visibility epoch, perspective, path, query) run the handler once. Waiters wait on an asyncio event and hold no thread-pool slot. |
| Degraded read: wait at most `LEAGUE_COLD_LOAD_BUDGET_S` (3 s) for a league's new files, otherwise serve that league's own previous files with `X-FI-Degraded: stale-files` | `app/db/league_files.py` (`pin_pair`), `league_scope.py` | Gives an honest "data may be older" signal instead of an unexplained wait or 503 after a publish or during an R2 outage. `lib/api.ts` exposes it as `stale: true`. |
| Failed-fetch back-off (30 s per league/version pair) | `league_files.py` | During an R2 outage, each instance tries once per 30 s, not on every request. |
| One jittered retry (0.2-0.5 s) for request-path R2 reads on network, 429 or 5xx failures; never on 403/404 or checksum mismatches | `league_files.py` (`R2Store`) | Absorbs a single transient blip, and costs at most one extra GET. |
| A full cache waits (8 s) for running requests to release files before answering 503 | `league_files.py` (`_make_room`) | Turns most overload 503s into short waits (30 failures to 4 above), and the byte bound is never exceeded. |
| `loading.tsx` for Matchup | `frontend/app/matchup/loading.tsx` | Every heavy route now has its own skeleton: Overview (root), Waivers, Trades, Trades → Discover (plus Suspense on the scan), Matchup, Players, Rankings. |

The isolation rules are unchanged. File keys are `leagues/<validated id>/<version>`. The degraded fallback is keyed by
the validated league id, so a league only ever falls back to its own previous pair. That pair must be strictly older than the pointer: after a rollback, a newer cached pair is forgotten and never served in its place. Response and single-flight keys
include the league, data and global versions, visibility epoch and perspective. Authorization still runs before any
cache lookup or file load. The tests are `tests/test_perf_guards.py` and the issue #201 block in `tests/test_league_scope.py`
(concurrent cold loads of two leagues with colliding ids through the app, single flight, degraded and recovery paths).

Sync frequency, the sync workers and provider calls are unchanged. The read path makes no provider calls. The only
request-path provider-side calls are the user-initiated connect checks and the best-effort drain dispatch in
`api/connect.py`, which already have their own timeouts.

## Free-tier budget (monthly quotas from `app/monitor/collect.py`, via `app/monitor/budgets.free_tier_quotas()`)

| Resource | Free quota | Impact of this change | New paid dependency |
|---|---|---|---|
| Vercel Function Invocations | 1,000,000 | **0.** No new routes or calls. Headers and timing ride on existing requests. | None |
| Vercel Fluid Active CPU | 4 h | **Lower under bursts.** Single flight runs each identical answer once (the 10-viewer burst computed 6 answers instead of up to 60). Timing costs microseconds per request. Logging is off by default. | None |
| Vercel Fluid Provisioned Memory | 360 GB-h | **About the same.** Wall time drops for stampedes and stale reads (at most 3 s waits). In overload, a request waits up to 8 s for cache room instead of a fast 503. | None |
| R2 Class B (GET) | 10,000,000 | **About the same.** At most +1 GET per transient failure. The 30 s back-off caps outage retries at 1 per pair per instance (before: one per request). When more leagues are open at once than one instance can cache, the room wait completes more loads and so re-downloads more (+37 GETs in the 25-league burst). | None |
| R2 Class A (PUT/LIST) | 1,000,000 | **0.** The API stays read-only, and publishing is unchanged. | None |
| R2 storage | 10 GB | **0.** No new objects. | None |
| Supabase database / accounts | 500 MB / 50,000 | **0.** No new queries, tables or migrations. The authorization query per request is unchanged. | None |
| GitHub Actions minutes | Free for public repositories (the repo's Actions logs are public) | **0.** No workflow changes. `perf_baseline.py` is run by hand and is not in CI. | None |

No new packages. The backend changes use only the standard library. The script uses `httpx`, already a dev
dependency, and the frontend change adds no dependency.

## Remaining risks

1. **The Discover scan under concurrency** (about 26 s for 10 leagues and 57 s for 25 on one instance) is CPU-bound and
   serial-ish in Python. It needs engine work (shared per-league precomputation or a smaller default search),
   beyond this issue's scope. Also, `app/api/trade_discovery.py` keeps a 4-entry module `OrderedDict` memo,
   keyed with the scope cache key (so per league), with no lock and no single flight across threads.
2. **Working sets larger than the file cache** (more than about 21 leagues of 16.5 MB open at once on one instance)
   thrash R2 and can still return 503 after the 8 s wait, because requests waiting for room occupy thread-pool
   slots that pinned requests need. Raising `LEAGUE_CACHE_MAX_BYTES` is limited by Vercel's 500 MB `/tmp`. The
   recommended follow-up is to split `_resolve` into authorization and file loading, and to gate cold loads with a
   small per-event-loop `asyncio.Semaphore` before entering the thread pool. Waiting loads would then hold no thread.
3. The `stale: true` flag reaches `ApiResult` but is not displayed yet. The sync pill already shows the older file's
   own sync times, because `/api/status` is read from the file that was served. Showing a "data may be older" line
   is a one-line change in `app/layout.tsx` (pass `status?.stale` to `SyncPill`), which is outside this change's
   file ownership.

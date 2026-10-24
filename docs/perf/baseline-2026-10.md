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

## Re-baseline after #277 and #281 (2026-10-23)

The same script, with two pages added: **Start/Sit** (`/api/start-sit`) and the header's **player search**
(`/api/players?q=alpha&limit=8`, a typed query at the search box's limit). An A/B on one machine, back to back:
A = 7fea582 (#235, the "after" above) and B = main at 30ea75d plus the two script rows. Two pairs were run, A then B and
then B then A, so drift in machine load cannot favour one side.

- Machine: Linux x86_64, Python 3.12.3, **4 CPUs**, a local **Postgres 16** cluster (not hosted Supabase), the simulated
  R2 link above (50 ms per object plus 30 MB/s). **Load average during the runs: 0.13-3.27 (median 1.45) for the first
  pair and 0.98-1.76 (median 1.35) for the second, 69 samples at 10 s.** The first baseline above ran at load 24-27, so
  compare A with B here, not with the tables above.
- Sample counts: each single-page row is the median of 3 fresh instances; each multi-league row is one burst of N
  leagues (N cold loads, then N warm, then N cached calls); one stampede of 10 viewers. Two pairs of runs per side.
- These are local, in-process numbers. They do not include the Next.js render or hosted network latency, and they are not
  an estimate of hosted latency.

### One league, per page (ms; run 1 / run 2)

| Page | Cold, A | Cold, B | Warm, A | Warm, B | Cached, A | Cached, B | Within budget |
|---|---:|---:|---:|---:|---:|---:|---|
| Overview | 1042 / 1061 | 1118 / 1147 | 171 / 172 | 211 / 214 | 34 / 39 | 34 / 42 | yes |
| Waivers | 956 / 955 | 1012 / 1021 | 86 / 88 | 99 / 108 | 21 / 24 | 24 / 22 | yes |
| Trades | 973 / 987 | 1055 / 1039 | 80 / 106 | 111 / 108 | 16 / 18 | 17 / 18 | yes |
| Trades → Discover | 2677 / 2922 | 2968 / 2843 | 1798 / 1909 | 1966 / 1959 | 22 / 25 | 28 / 23 | yes |
| Matchup | 979 / 945 | 1005 / 1012 | 88 / 81 | 88 / 106 | 19 / 18 | 18 / 18 | yes |
| Players | 1031 / 940 | 984 / 991 | 52 / 65 | 63 / 82 | 16 / 21 | 18 / 22 | yes |
| **Start/Sit** (new) | 1100 / 970 | 1052 / 1053 | 103 / 89 | 118 / 126 | 21 / 19 | 19 / 18 | yes |
| **Player search** (new) | 1002 / 896 | 966 / 949 | 26 / 24 | 30 / 29 | 8 / 7 | 7 / 7 | yes |

Every cold page costs 2 R2 GETs on both sides.

### Leagues loading at once (one instance; run 1 / run 2)

| Leagues | Overview cold p95, A | Overview cold p95, B | Overview warm p95, A | Overview warm p95, B | Discover scan warm p50, A | Discover scan warm p50, B | Failed calls A / B |
|---:|---|---|---|---|---|---|---|
| 1 | 1190 / 1038 | 1174 / 1149 | 181 / 185 | 223 / 196 | 1781 / 2059 | 2064 / 1855 | 0, 0 / 0, 0 |
| 10 | 3142 / 2997 | 3553 / 3384 | 1955 / 1994 | 2386 / 2167 | 20836 / 22478 | 19169 / 19733 | 0, 0 / 0, 0 |
| 25 | 7978 / 7294 | 9200 / 8263 | 6731 / 6076 | 7272 / 6732 | 49223 / 47655 | 46849 / 47378 | 4, 4 / 3, 3 |

Stampede (10 viewers, one cold league): A 1417 / 1412 ms, B 1617 / 1484 ms; 2 R2 GETs and 54 shared answers on every run.

What it says:

- **Start/Sit and player search are within every budget**, and player search is the cheapest page (about 30 ms warm).
- **Warm compute grew since #235**, consistently in both pairs: Overview about +40 ms warm (about +24%) for one league;
  Overview cold p95 about +13% for 10 leagues and +14% for 25; Start/Sit, Waivers and Trades about +10-30 ms warm. The
  Discover scan, cached answers, R2 traffic and the 25-league failure count (3-4 `CacheFull` 503s) did not get worse.
  It is within budget. A bisect over the 80 first-parent commits (warm single-league Overview, 15 samples per step)
  points at #276 (#254 phase 2: per-row actual-points source and honest source labels), but a repeat of #276's
  parent was also slow once, so the about 30 ms step is near this machine's noise and is not confirmed. The numbers,
  the bisect table and the deterministic next step (compare one profiled warm request at both commits) are in **#331**;
  any fix must leave results identical.
- **Still open, both sides: the 25-league burst returns 3-4 `CacheFull` 503s** because the working set exceeds one
  instance's file cache. This is remaining risk 2 below (gate cold loads with a small per-event-loop semaphore so
  waiting loads hold no thread), tracked in **#331**.

## Cold loads wait on the event loop (#331 risk 2, 2026-10-24)

`league_scope` used to run authorization and the whole file load (download, or waiting up to 8 s for room in a full
cache) in one thread-pool slot. Now authorization and a cached-pair pin share one thread hop. A cache miss waits on the
event loop: per (league, data, global) pair one leader takes one of `LEAGUE_COLD_LOADS` (default 4) permits per loop
and loads in the pool; the others wait for it, then pin from the cache, read the same previous files when the leader
was served those, or answer 503 when it failed. Waiting for a permit or a leader ends after 20 s with a 503.

Same script, machine and fixture, main 26f7326 (A) then the gate (B), back to back. Load average 1.06-1.36 during A
and 1.36-1.19 during B. Each row is one run; single pages are medians of 3 fresh instances.

| Leagues at once | Overview cold p50 / p95, A → B | Overview warm p50 / p95 | Cached p50 / p95 | R2 GETs | Failed calls |
|---:|---|---|---|---:|---:|
| 1 | 1177 / 1177 → 1086 / 1086 | 154 / 154 → 163 / 163 | 41 / 41 → 36 / 36 | 2 → 2 | 0 → 0 |
| 10 | 3056 / 3136 → **2525 / 2935** | 1669 / 1758 → 1617 / 1756 | 406 / 472 → 393 / 469 | 11 → 11 | 0 → 0 |
| 25 | 6190 / 7666 → **4543 / 6105** | 4230 / 5656 → 4067 / 4873 | 2042 / 2763 → **1115 / 2328** | 121 → **50** | 4 → 4 |

Single-page cold, warm and cached times are within noise of each other on every page (all within budget). The Discover
scan at 25 leagues: p50 47616 → 41960 ms, p95 51746 → 51719 ms.

- **Fewer re-downloads.** At 25 leagues R2 GETs drop from 121 to 50 (2.0 GB to 0.8 GB). Loads no longer evict each
  other's files while they queue in the pool, so the 25-league cold burst is 27% faster at the median and 20% at p95.
- **The 503s are unchanged, and they are not the Overview burst.** In both runs the Overview cold, warm and cached
  bursts have 0 failed calls. All 4 failures are `CacheFull` 503s on `/api/trades/discover/summary` in the trade-scan
  burst. That is capacity, not thread starvation (remaining risk 2, updated).

## Remaining risks

1. **The Discover scan under concurrency** (about 26 s for 10 leagues and 57 s for 25 on one instance) is CPU-bound and
   serial-ish in Python. It needs engine work (shared per-league precomputation or a smaller default search),
   beyond this issue's scope. `app/api/trade_discovery.py` keeps a 4-entry module `OrderedDict` memo, keyed with the
   scope cache key (so per league); since #311 its lookup, insert and trim run under a lock (a concurrent eviction no
   longer raises), but there is still no single flight across threads, so two requests can build the same search.
2. **Working sets larger than the file cache** (more than about 21 leagues of 16.5 MB open at once on one instance).
   The thread-pool part is fixed (#331 risk 2, below): cold loads wait on the event loop, so waiting loads hold no
   thread and the cache thrashes far less. What remains is capacity: the 3-4 `CacheFull` 503s are all
   `/api/trades/discover/summary` calls in the 25-league trade-scan burst. 25 Discover scans pin about 413 MB, over
   the 350 MB bound, for about 45 s each (they are CPU-bound), so the last leagues find no room within the 8 s wait.
   Gating cannot remove those. The options are a larger `LEAGUE_CACHE_MAX_BYTES` (Vercel's `/tmp` is 500 MB), a
   Discover scan that does not hold its files for the whole search, or a shorter scan (remaining risk 1).
3. The `stale: true` flag reaches `ApiResult` but is not displayed yet. The sync pill already shows the older file's
   own sync times, because `/api/status` is read from the file that was served. Showing a "data may be older" line
   is a one-line change in `app/layout.tsx` (pass `status?.stale` to `SyncPill`), which is outside this change's
   file ownership.

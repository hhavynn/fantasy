# Phase 0b report: free object storage for DuckDB files

Run 2026-10-20 from the throwaway Vercel project `fi-md-spike` (Python Function, `iad1`), duckdb 1.5.6.
Files: today's database compacted with `COPY FROM DATABASE` (the 1.3 GB local file is mostly free
blocks): **95 MB** full, or **31 MB league + 47 MB global** split. Raw results: `results_blob.json`
(its Supabase rows failed on a full `/tmp`; superseded by `results_supabase.json`).

## Verdict: GO with Cloudflare R2

**Only Cloudflare R2's free tier can pay for full-file downloads**, because its egress is free. One R2
stream from `iad1` is slower than Blob (~30 MB/s), so the client downloads large objects with parallel
ranged GETs, which meets the target (95 MB: p50 ~0.4–1.0 s, p95 ~1.0–1.4 s with 4–16 parts; the
shipped client uses 8). `r2_bench.py` (presigned URLs, so the probe never held a token) produced the R2
rows; `results_r2*.json` hold the raw numbers.

## Measurements (p50 / p95, ms, server-side in the Function)

| Store | File | Full download | Open | 8-query page mix | Range-read attach | Range-read queries | Range bytes fetched |
|---|---|---|---|---|---|---|---|
| Vercel Blob (private) | full 95 MB | 335 / 1,608 | 47 / 55 | 21 / 67 | 351 / 590 | 64 / 117 | 102.8 MB (~100%) |
| Vercel Blob (private) | league 31 MB | 119 / 801 | 20 / 21 | 16 / 46 | 276 / 299 | 68 / 90 | 34.6 MB |
| Vercel Blob (private) | global 47 MB | 156 / 1,590 | 38 / 41 | 12 / 12 | 385 / 454 | 30 / 75 | 53.0 MB |
| Supabase Storage (private, signed URL) | full 95 MB | **upload rejected (50 MB max file on free)** | | | | | |
| Supabase Storage | league 31 MB | 123 / 863 | 17 / 23 | 21 / 126 | 461 / 1,063 | 175 / 201 | 34.6 MB |
| Supabase Storage | global 47 MB | 160 / 980 | 35 / 39 | 17 / 18 | 1,054 / 1,934 | 57 / 70 | 53.0 MB |
| Cloudflare R2 (private, presigned, 1 stream) | full 95 MB | 2,971 / 4,042 | 38 / 49 | 34 / 50 | | | |
| Cloudflare R2, 1 stream | league 31 MB | 1,403 / 2,170 | 14 / 18 | 19 / 20 | | | |
| Cloudflare R2, 1 stream | global 47 MB | 734 / 2,355 | 33 / 34 | 12 / 18 | | | |
| Cloudflare R2, 4 ranged parts | full 95 MB | 1,000 / 1,370 | | | | | |
| Cloudflare R2, 8 ranged parts | full 95 MB | 437 / 2,569 (one outlier, n=5) | | | | | |
| Cloudflare R2, 16 ranged parts | full 95 MB | 972 / 1,050 | | | | | |

`httpfs` load on a fresh instance: ~125–140 ms (p95 up to ~500 ms).

## Findings

1. **Range reads save nothing.** DuckDB's remote `ATTACH` fetched the whole file (≈100% of bytes, 16–31
   requests) even for an 8-query page, and each new connection fetches again. Full download once per
   instance, then local reads, is cheaper and faster.
2. **`/tmp` is 500 MB and shared by everything in the instance.** The test filled it with unique names
   ("No space left on device"). The read path keeps a byte-bounded cache, downloads to a temp name and
   renames atomically, and evicts old versions.
3. **Transfer budget decides the store.** Each cold instance downloads ~80–95 MB.
   - Vercel Blob Hobby: 10 GB transfer + 2,000 uploads/month; exceeding locks Blob for 30 days.
     ≈ 100 cold downloads/month. Out.
   - Supabase free: 50 MB max file, 5 GB uncached egress. Out (and the full file does not fit).
   - R2 free: 10 GB-month storage, 1M writes, 10M reads, **free egress**. In.
4. **Read-only credential.** R2 API tokens can be scoped to Object Read on one bucket, so the API can
   hold a token that cannot write or delete. (Blob's store token is read-write; Supabase needs signed
   URLs minted with the service key.)

5. **R2 needs parallel ranges.** A single stream manages ~30 MB/s from `iad1` (the bucket was created
   with automatic location); 4–16 concurrent ranged GETs reach 70–200 MB/s. Uploads from the laptop:
   95 MB in ~8 s.

## Next

- Phase 1a ships the R2 read path with 8-part ranged downloads (PR #144). First real publish:
  `snapshots/20261020T063035.duckdb`, 94.4 MB, downloaded intact.
- Production switched 2026-10-20: the API serves `snapshots/<stamp>.duckdb` from R2 (verified: requests
  return 200 and MotherDuck shows no API queries after the deploy); the MotherDuck env vars were removed.
- Cleaned up: the `fi-spike-blob` Blob store, the Supabase `spike` bucket, the `fi-md-spike` Vercel
  project and the R2 `spike/` objects are deleted.

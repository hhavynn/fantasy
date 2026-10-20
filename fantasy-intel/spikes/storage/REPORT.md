# Phase 0b report: free object storage for DuckDB files

Run 2026-10-20 from the throwaway Vercel project `fi-md-spike` (Python Function, `iad1`), duckdb 1.5.6.
Files: today's database compacted with `COPY FROM DATABASE` (the 1.3 GB local file is mostly free
blocks): **95 MB** full, or **31 MB league + 47 MB global** split. Raw results: `results_blob.json`
(its Supabase rows failed on a full `/tmp`; superseded by `results_supabase.json`).

## Verdict

Every store is fast enough from `iad1`; **only Cloudflare R2's free tier can pay for full-file
downloads**, because its egress is free. R2 latency is pending Havyn's Cloudflare account; the decision
does not depend on it unless R2 is unexpectedly slow (re-run `storage_bench.py` with an `r2` series).

## Measurements (p50 / p95, ms, server-side in the Function)

| Store | File | Full download | Open | 8-query page mix | Range-read attach | Range-read queries | Range bytes fetched |
|---|---|---|---|---|---|---|---|
| Vercel Blob (private) | full 95 MB | 335 / 1,608 | 47 / 55 | 21 / 67 | 351 / 590 | 64 / 117 | 102.8 MB (~100%) |
| Vercel Blob (private) | league 31 MB | 119 / 801 | 20 / 21 | 16 / 46 | 276 / 299 | 68 / 90 | 34.6 MB |
| Vercel Blob (private) | global 47 MB | 156 / 1,590 | 38 / 41 | 12 / 12 | 385 / 454 | 30 / 75 | 53.0 MB |
| Supabase Storage (private, signed URL) | full 95 MB | **upload rejected (50 MB max file on free)** | | | | | |
| Supabase Storage | league 31 MB | 123 / 863 | 17 / 23 | 21 / 126 | 461 / 1,063 | 175 / 201 | 34.6 MB |
| Supabase Storage | global 47 MB | 160 / 980 | 35 / 39 | 17 / 18 | 1,054 / 1,934 | 57 / 70 | 53.0 MB |
| Cloudflare R2 | all | pending account | | | | | |

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

## Next

- Havyn: create the Cloudflare account, bucket `fantasy-intel`, an Object Read & Write token for the
  publisher and an Object Read token for the API; add `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY` (read/write) to `fantasy-intel/.env`.
- Then: add an `r2` series to `storage_bench.py`, run it, fill the table, and switch production
  (Phase 1a).
- Cleanup after Phase 1a: delete the `fi-spike-blob` Blob store, the Supabase `spike` bucket and the
  `fi-md-spike` Vercel project.

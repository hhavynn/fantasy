# Phase 0: MotherDuck validation spike

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decide go/no-go on Option B (one MotherDuck database per league) with measurements, not assumptions.

**Architecture:** Throwaway scripts in `fantasy-intel/spikes/motherduck/` create `spike_*` databases, run experiments, append JSON results to `spikes/motherduck/results/*.json`, and a summary goes to `spikes/motherduck/REPORT.md`. A throwaway Vercel project `fi-md-spike` measures in-region latency. Master plan: [2026-10-19-multi-league-platform.md](2026-10-19-multi-league-platform.md).

**Tech Stack:** Python 3 + `duckdb` (the backend's version), MotherDuck, Vercel Python Functions.

## Global Constraints

- This is a measurement spike: each task states the exact experiment and its expected-vs-observed record instead of a unit test. Spike code never ships; nothing under `backend/app/` changes.
- Tokens are read from `fantasy-intel/.env` (`MOTHERDUCK_API_TOKEN` = the hosted API's token type, `MOTHERDUCK_TOKEN` = laptop) and never printed, logged, or written to results.
- Only databases named `spike_*` are created, written or dropped. `fantasy_intel` and `my_db` are never touched.
- Results record environment (duckdb version, region, token type label), timestamps and raw timings.

## File structure

| File | Responsibility |
|---|---|
| `spikes/motherduck/common.py` | Load tokens from `.env`, connect helpers, timer, `record(name, payload)` result writer |
| `spikes/motherduck/tables.py` | League vs global table classification for the spike copy |
| `spikes/motherduck/setup_dbs.py` | Build `spike_fi_global` and `spike_lg_001..060` from a local copy of `fantasy.duckdb` |
| `spikes/motherduck/isolation.py` | Cross-database access attempts, config-lock guard, read-only mechanisms |
| `spikes/motherduck/consistency.py` | Atomic multi-table write + kill, fence, cross-DB snapshot, reader lag |
| `spikes/motherduck/concurrency.py` | Parallel writers (different and same DB) with readers |
| `spikes/motherduck/latency_local.py` | Laptop connect/attach/query timings |
| `spikes/motherduck/vercel/` | `api/probe.py` + `requirements.txt` + `vercel.json` for in-region timings |
| `spikes/motherduck/sync_duration.py` | Time a full Los Subies ESPN fetch + derived compute against a scratch DB |
| `spikes/motherduck/migrate_fanout.py` | Apply one additive schema change to all 60 league DBs |
| `spikes/motherduck/teardown.py` | Drop every `spike_*` database |
| `spikes/motherduck/REPORT.md` | Findings, numbers, go/no-go |

### Task 1: Harness and spike databases
- [ ] `common.py`, `tables.py`, `setup_dbs.py`, `teardown.py`.
- [ ] Classification: league tables = `leagues, league_members, fantasy_teams, fantasy_players, matchups, draft_picks, lineup_snapshots, roster_history, transactions, transaction_items, activity_messages, espn_player_weekly_points, espn_projection_history, player_ros_projections, player_ownership_history, waiver_recommendations, team_waiver_recommendations, team_position_needs, team_week_lineup_projections, team_ros_lineup_projections, manager_metrics, manager_classifications, sync_runs, schema_meta`; everything else is global.
- [ ] Run `setup_dbs.py --leagues 60`; record per-league and global sizes, upload time, and whether creating 60 databases hits any limit.
- [ ] Each league DB gets `_sync_meta(fence BIGINT, data_version BIGINT, fi_global_version VARCHAR, committed_at TIMESTAMP)` with one row `(0, 0, 'v0', now())`.

### Task 2: Isolation (real SQL, API token type)
From `duckdb.connect('md:spike_lg_001?attach_mode=single', config={'motherduck_token': <API token>})` record success/failure + error text for each:
- [ ] `SELECT database_name FROM duckdb_databases()` and `SHOW ALL DATABASES` / `FROM md_list_databases()` (what is visible).
- [ ] `ATTACH 'md:spike_lg_002'`; `SELECT count(*) FROM spike_lg_002.main.fantasy_teams`.
- [ ] Without attaching: `SELECT count(*) FROM spike_lg_002.main.fantasy_teams` (lazy attach?).
- [ ] `USE spike_lg_002`; `CREATE DATABASE spike_tmp_x`; `DROP DATABASE spike_tmp_x`; `INSERT INTO spike_lg_002.main._sync_meta VALUES (...)`.
- [ ] Repeat all of the above after `SET enable_external_access = false; SET lock_configuration = true;` on a fresh connection (does it block MotherDuck `ATTACH`, and does `fi_global` still work if attached *before* locking?).
- [ ] Read-only mechanisms: `ATTACH 'md:spike_fi_global' (READ_ONLY)` then attempt a write; `access_mode=read_only` connection config; `CREATE SHARE spike_lg_001_share FROM spike_lg_001` + attach the share URL and attempt a write; note read-scaling token availability on the current plan.
- [ ] `search_path`: `SET search_path = 'spike_lg_001.main,spike_fi_global.main'`; unqualified `SELECT` from one league table and one global table.

### Task 3: Consistency
- [ ] Atomic: in one transaction on `spike_lg_003`, `DELETE`+`INSERT` 5 tables and bump `_sync_meta`; kill the process (`os._exit`) before `COMMIT`; a new connection sees the old row counts and old `data_version`.
- [ ] Fence: writer A takes fence 5, writer B takes fence 6; B commits (`UPDATE _sync_meta SET fence=6 WHERE fence<6`); A then runs `UPDATE ... SET fence=5 WHERE fence<5` → 0 rows → A rolls back. Record that A's data is absent.
- [ ] Cross-DB snapshot: reader `BEGIN` on a connection with league + global attached, read both versions; concurrently a writer bumps both; reader re-reads inside the same transaction — same values?
- [ ] Reader lag: writer (laptop token) commits `data_version = n`; a separate process with the API token polls every 100 ms until it sees `n`; 20 trials; record p50/p95/max.

### Task 4: Concurrency
- [ ] 5 processes each writing a different league DB (transaction of 5 tables, 10 iterations) while 5 reader processes loop queries; record write times, errors, reader errors.
- [ ] 2 processes writing the same league DB concurrently; record conflict errors and final state.

### Task 5: Latency
- [ ] Laptop: 20 trials each of cold (new process) connect+attach global+`search_path`+first engine-like query (standings join), warm (cached connection, new cursor) on rotating leagues; p50/p95.
- [ ] Vercel: deploy `fi-md-spike` (Python function, region = the backend project's region), env `MOTHERDUCK_TOKEN` from `MOTHERDUCK_API_TOKEN` piped without printing; endpoint runs the same measurement for `?mode=cold|warm&league=NNN`; 30 calls each; record p50/p95 including cold function starts.

### Task 6: Sync duration and migrations
- [ ] Run the existing ESPN sync for Los Subies into a scratch local DuckDB file (no MotherDuck writes) and time fetch and compute stages separately; then time copying the league tables into `spike_lg_060` in one transaction.
- [ ] `migrate_fanout.py`: `ALTER TABLE _sync_meta ADD COLUMN spike_col INTEGER` across 60 DBs sequentially and with 8 threads; record total time and errors.

### Task 7: Queues, cost, report, teardown decision
- [ ] Vercel Queues: check availability for Python on this team (docs + a publish/consume attempt in `fi-md-spike` if available).
- [ ] Cost: storage per league from Task 1; compute per sync (Task 6) and per page view (Task 5); MotherDuck plan and published pricing; projection at 100 and 1,000 leagues.
- [ ] Write `REPORT.md` with every result, the go/no-go call against the master plan's Phase 0 acceptance list, and blockers.
- [ ] Ask Havyn before teardown (keep databases if Phase 1 will reuse them).

# Soccer stats implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Soccer trends carry real, sourced numbers and a real event window.

**Architecture:** PR 3 of 5, stacked on the adapter PR. `sync-stats` fills `data/soccer/soccer.duckdb` one source at
a time; `SoccerEnricher` reads it read-only and point-in-time. See
`docs/superpowers/specs/2026-10-30-soccer-shorts-account-design.md`.

**Tech Stack:** Python 3.11+, DuckDB, httpx, pytest, ruff, uv.

## Global Constraints

- `fantasy.duckdb` is never touched. No new dependencies. `social-intel/1.0` stays unchanged.
- Every stat has `label`, `value`, `source` and `as_of`. Nothing dated after `now` is used.
- A failed source keeps its previous rows.

---

### Task 1: Sync

**Files:** Create `sports/soccer/stats.py`. Modify `cli.py`, `sports/soccer/clubs.py`. Test `tests/test_soccer.py`.

- [ ] Test CSV parsing, MLS season filtering, player parsers and that a failed source keeps its rows.
- [ ] Implement `sync` and `sync-stats`. Report unmapped clubs instead of dropping them silently.

### Task 2: Enrichment and event window

**Files:** Modify `sports/soccer/stats.py`, `sports/soccer/adapter.py`, `cli.py`. Test `tests/test_soccer.py`.

- [ ] Test sourced club and player stats, point-in-time results, no coverage without data, and a position only when
  every club in the league is present.
- [ ] Implement `SoccerEnricher` and use the next fixture for `event_window`, falling back to next Saturday.

### Task 3: Live check

- [ ] Run `sync-stats` and `rank` against scratch paths. All leagues must map every club.

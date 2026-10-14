# Manager scouting implementation plan

**Goal:** Deliver the user's supplied manager scouting specification as a read-only, evidence-backed report for every team, independently of the concurrent trade branch.

**Architecture:** A new analytics module consumes stored manager metrics/classifications, current-week snapshots and existing need rows. Reuse `load_trades` for completed, deduplicated trade facts. A dedicated GET router is registered in `main.py`. The existing manager detail page fetches both endpoints concurrently and preserves its useful roster, move, weekly composition and raw evidence views.

**Tech stack:** FastAPI, DuckDB, pytest, Next.js server components, existing CSS tokens.

**Spec:** User attachment `Pasted text.txt`, supplied 2026-10-14; requirements 1–15.

## Global constraints

- Branch `feature/v2-manager-scouting` from freshly fetched `origin/main`, separate managed worktree.
- Never edit protected lineup/projections/waivers/schema/connection files, metric formulas, or trade UI; no migration or schema version change.
- Current roster state is distinct from historical tendency. Missing history is INSUFFICIENT, never LOW by default.
- Every headline includes evidence and confidence. No predictions or acceptance probabilities.
- Preserve stored need formula and stored classification rules.

## Review focus

- Missing current snapshots must be unknown rather than an empty roster.
- League comparisons use observed teams, include zero positions, and require sufficient acquisition denominators.
- Stale metrics and missing classifications remain visible as limitations, not invented labels.
- Completed trades must deduplicate transaction/activity sources and exclude pending/canceled records.
- Mobile tables scroll within sections; evidence and empty states remain readable.

## Tasks

### Task 1: Read-only report and API

**Files:** new `backend/app/analytics/manager_scouting.py`, `backend/app/api/manager_scouting.py`, `backend/tests/test_manager_scouting.py`; modify only `backend/app/main.py` for wiring.

**Interface:** `build_manager_scouting(con, team_id: int) -> dict | None`, exposed at `GET /api/managers/{team_id}/scouting`; missing team returns 404, missing league 503.

- [x] Add fixture-backed failing tests for stored labels, missing samples, weighted position ratios, missing league activity, current composition/IR, historical separation, need passthrough, starter rotation, zero/completed trades, deduplication, scope, evidence, read-only behavior and API errors.
- [x] Run targeted tests to confirm failure, then implement report and router.
- [x] Run targeted tests until green.

### Task 2: Scouting page

**Files:** manager `[teamId]/page.tsx`; new colocated scouting view/CSS and `lib/manager-scouting.ts` types. Avoid shared types and global styles.

**Consumes:** Task 1 report; preserves existing manager detail payload.

- [x] Add compact summary, current roster construction, historical position/waiver/streaming tables, stored needs and factual trade history.
- [x] Keep metric definitions, classifications, weekly construction, current roster, failed claims and moves accessible through disclosure sections.
- [x] Verify lint/build and desktop/mobile render, including missing data and endpoint failure.

### Task 3: Real league audit and delivery

**Files:** new `scripts/check_manager_scouting.py`, `docs/manager-scouting.md`, `docs/manager-scouting-los-subies.md`.

- [x] Run all 12 reports read-only, document strongest supported tendency, biggest need, completed trades and conservative trade confidence; spot-check user's team and move/trade extremes.
- [x] Run full `uv run pytest`, `npm run lint`, `npm run build`.
- [x] Review branch, fix material findings, fetch origin and compare overlapping paths against concurrent branch without rebasing.
- [x] Commit and push requested branch; do not merge.

## Execution record

Baseline: `origin/main` at `39b1a0f`; 213 tests pass. Execute inline under the user's explicit build/commit/push instruction. No additional design approval is required by session instructions.

Tasks 1–3 implemented and verified. Final suite: 242 passed (29 new), one pre-existing Starlette/httpx deprecation warning. Frontend lint/build pass. Desktop/390px browser checks pass; all 12 Los Subies reports were audited read-only. Independent review found one Important stale-evidence issue; four missing/mismatch regressions failed before the evidence-consistency fix and now pass. No actionable minors were deferred. Sparse streaming evidence also has a RED→GREEN crash regression. No protected paths or formulas changed.

Decisions: use >=5/20 known-position add samples and >=2 teams for ratios; use conservative completed-trade sample confidence thresholds documented in the module and report; preserve original stored classification metadata while withholding missing/mismatched evidence from active labels. These display rules add no metric-engine changes or preference predictions.

# Weekly Start/Sit Command Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reliable, read-only Weekly Start/Sit Command Center for ESPN fantasy football that compares a manager's actual ESPN lineup against their highest-projected lock-constrained lineup, identifying actionable start/sit moves without double counting or violating game locks.

**Architecture:** Pure, modular analytics engine in `backend/app/analytics/start_sit.py` that computes player kickoff locks, preserves locked starters in their exact ESPN slot instances, and optimizes remaining changeable slots using `backend/app/analytics/lineup.py`. Exposed via `backend/app/api/start_sit.py` (FastAPI) and consumed by Next.js in `frontend/app/start-sit/page.tsx`.

**Tech Stack:** Python 3.12, DuckDB, FastAPI, Pytest, Next.js 16 (App Router, Turbopack), React 19, TypeScript 5.

## Global Constraints

- Never modify: `trade_practical.py`, `trade_assets.py`, `trade_discovery.py`, `trades.py`, waiver recommendation algorithms, or ESPN/NFL ingestion logic.
- Reuse `lineup.assign_lineup()` and `lineup.lineup_slots()` as pure read-only dependencies.
- No network requests during page rendering; all data reads from stored DuckDB database.
- Projection source is explicit: `projection_source: "ESPN"` (future-ready for Claude's model).
- Headline net projected gain is strictly `recommended_total - actual_total`; never sum isolated pairwise differences.
- A player whose game kicked off can never be recommended to be benched. A bench player whose game kicked off can never be recommended to enter the lineup.
- Missing schedule rows must never be treated as a confirmed bye unless schedule coverage reliably establishes it.
- Missing ESPN projection is unknown (`None`), not zero; true `0.0` is a measured zero.

---

### Task 1: Kickoff Lock Detection & Schedule Coverage

**Files:**
- Create: `fantasy-intel/backend/app/analytics/start_sit.py`
- Test: `fantasy-intel/backend/tests/test_start_sit.py`

**Interfaces:**
- Consumes: `trade_data.kickoff_utc(gameday, gametime)` from `backend/app/analytics/trade_data.py`.
- Produces:
  - `PlayerLock`: dataclass with `status: str` (`"LOCKED" | "UNLOCKED" | "UNKNOWN" | "BYE" | "TEAMLESS"`), `kickoff_utc: datetime | None`, `opponent: str | None`, `is_home: bool | None`, `has_final_score: bool`, `reason: str`.
  - `detect_player_locks(con, season: int, week: int, pro_teams: list[str | None], now: datetime) -> dict[str, PlayerLock]`
  - `verify_schedule_coverage(con, season: int, week: int) -> bool`

- [ ] **Step 1: Write failing tests for lock detection**

Write test cases in `backend/tests/test_start_sit.py` covering:
- Kickoff in the past -> `LOCKED`
- Final score present even if kickoff missing -> `LOCKED`
- Kickoff in future -> `UNLOCKED`
- Kickoff missing/unparseable -> `UNKNOWN`
- Player team not in schedule when week has full schedule -> `BYE`
- Player team not in schedule when schedule table missing/empty -> `UNKNOWN` (no false bye!)
- Player with no pro team (`None` or `"FA"`) -> `TEAMLESS`

- [ ] **Step 2: Run pytest to verify failures**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_lock_status" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.analytics.start_sit'`

- [ ] **Step 3: Implement lock detection in `start_sit.py`**

Implement `PlayerLock`, `verify_schedule_coverage`, and `detect_player_locks`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_lock_status" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/analytics/start_sit.py backend/tests/test_start_sit.py
git commit -m "feat(analytics): add kickoff lock detection and schedule coverage verification"
```

---

### Task 2: Actual Lineup Loading & Slot Instance Preservation

**Files:**
- Modify: `fantasy-intel/backend/app/analytics/start_sit.py`
- Test: `fantasy-intel/backend/tests/test_start_sit.py`

**Interfaces:**
- Consumes: `lineup_snapshots`, `leagues.roster_slots`, `DEFAULT_POSITION_MAP`, `SLOT_MAP`.
- Produces:
  - `SlotInstance`: dataclass `(slot_id: str, slot_type: str, index: int, is_flex: boolean)` e.g. `SlotInstance("RB_0", "RB", 0, False)`, `SlotInstance("FLEX_0", "FLEX", 0, True)`.
  - `load_actual_lineup(con, league_id: int, season: int, scoring_period: int, team_id: int) -> tuple[list[dict], list[dict], list[dict]]`: returns `(starters, bench, ir)` with assigned slot instances.
  - `preserve_locked_starters(actual_starters: list[dict], player_locks: dict[int, PlayerLock], all_slot_instances: list[SlotInstance]) -> tuple[list[dict], list[SlotInstance], set[int]]`: returns `(locked_starter_assignments, remaining_open_slot_instances, locked_starter_player_ids)`.

- [ ] **Step 1: Write failing tests for actual lineup loading & slot preservation**

Write tests in `backend/tests/test_start_sit.py`:
- Duplicate slot handling: preserves both `RB_0` and `RB_1` correctly.
- Locked starter occupying `FLEX` retains `FLEX` slot instance, leaving only fixed slots open.
- Locked starter occupying `RB` retains `RB` slot instance, leaving other `RB` and `FLEX` open.
- Unlocked starters are freed up to compete in the optimizer.

- [ ] **Step 2: Run pytest to verify failures**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_slot_preservation" -v`
Expected: FAIL

- [ ] **Step 3: Implement slot preservation logic**

Implement stable `SlotInstance`, `expand_slot_instances`, and `preserve_locked_starters`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_slot_preservation" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/analytics/start_sit.py backend/tests/test_start_sit.py
git commit -m "feat(analytics): implement actual lineup loading and slot instance preservation"
```

---

### Task 3: Lock-Constrained Lineup Optimization & Mathematical Benchmark

**Files:**
- Modify: `fantasy-intel/backend/app/analytics/start_sit.py`
- Test: `fantasy-intel/backend/tests/test_start_sit.py`

**Interfaces:**
- Consumes: `assign_lineup` from `backend/app/analytics/lineup.py`.
- Produces:
  - `optimize_lock_constrained_lineup(roster: list[dict], slot_instances: list[SlotInstance], locked_assignments: list[dict], locked_bench_ids: set[int], unavailable_ids: set[int], ir_ids: set[int], projections: dict[int, float]) -> dict`:
    returns `{"recommended_lineup": list[dict], "unconstrained_lineup": list[dict], "actual_total": float | None, "recommended_total": float | None, "unconstrained_total": float | None, "is_complete": bool, "missing_ids": list[int]}`.

- [ ] **Step 1: Write failing tests for constrained optimization**

Write tests in `backend/tests/test_start_sit.py`:
- Invariant: When no games are locked, recommended lineup matches unconstrained `assign_lineup`.
- Locked starter with lower projection than bench player is NOT replaced.
- Locked bench player with higher projection than starter is NOT started.
- Unlocked starter with lower projection than unlocked bench player IS replaced.
- IR players (`on_ir == True`) never enter lineup.
- Verified NFL-ineligible players (`unavailable_weeks`) never enter lineup.
- True zero projection (`0.0`) is startable if no better player exists, and does not mark week incomplete.
- Missing projection (`None`) on a lineup-relevant player marks week incomplete (`is_complete = False`).

- [ ] **Step 2: Run pytest to verify failures**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_constrained_optimization" -v`
Expected: FAIL

- [ ] **Step 3: Implement constrained optimization adapter**

Map open `SlotInstance`s to slot type strings, pass eligible unlocked players to `assign_lineup`, and recombine with locked assignments.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_constrained_optimization" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/analytics/start_sit.py backend/tests/test_start_sit.py
git commit -m "feat(analytics): implement lock-constrained lineup optimization and unconstrained benchmark"
```

---

### Task 4: Decision Attribution, Coordinated Changes & Actionability Classification

**Files:**
- Modify: `fantasy-intel/backend/app/analytics/start_sit.py`
- Test: `fantasy-intel/backend/tests/test_start_sit.py`

**Interfaces:**
- Consumes: Actual Lineup, Recommended Lineup, Player Locks.
- Produces:
  - `StartSitDecision`: dataclass representing a recommendation (action: `"START" | "BENCH" | "COORDINATED_SWAP"`, actionability: `"ACTIONABLE" | "LOCKED" | "UNCERTAIN" | "IR_BLOCKED" | "NO_CHANGE"`, start_players: list, bench_players: list, slot: str, net_gain: float | None, explanation: str).
  - `generate_start_sit_decisions(actual_starters: list[dict], recommended_starters: list[dict], player_locks: dict[int, PlayerLock], projections: dict[int, float], ir_ids: set[int]) -> list[StartSitDecision]`

- [ ] **Step 1: Write failing tests for decision attribution**

Write tests in `backend/tests/test_start_sit.py`:
- 1-for-1 swap: entering player paired with displaced player; gain equals delta.
- Already optimal: returns single `NO_CHANGE` decision with 0.0 gain.
- Multi-player FLEX cascade (e.g. 2 entering, 2 displaced across RB/WR/FLEX): grouped as a coordinated decision; net gain equals total lineup difference (no double counting).
- Actionability classification:
  - Both players unlocked -> `ACTIONABLE`
  - Either player locked -> `LOCKED`
  - Either player kickoff unknown -> `UNCERTAIN`
  - Incomplete projections -> `UNCERTAIN`

- [ ] **Step 2: Run pytest to verify failures**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_decision_attribution" -v`
Expected: FAIL

- [ ] **Step 3: Implement decision attribution & explanation logic**

Implement set difference comparison, direct vs coordinated grouping, and actionability categorization.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_decision_attribution" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/analytics/start_sit.py backend/tests/test_start_sit.py
git commit -m "feat(analytics): add decision attribution and actionability classification"
```

---

### Task 5: Start/Sit FastAPI Endpoint

**Files:**
- Create: `fantasy-intel/backend/app/api/start_sit.py`
- Modify: `fantasy-intel/backend/app/main.py`
- Test: `fantasy-intel/backend/tests/test_start_sit.py`

**Interfaces:**
- Route: `GET /api/start-sit`
- Query Params: `team_id: int | None = None`, `week: int | None = None`
- Consumes: `load_start_sit_evaluation(con, league_id, season, team_id, week, now)`

- [ ] **Step 1: Write failing API integration tests**

Write tests in `backend/tests/test_start_sit.py`:
- `GET /api/start-sit`: defaults to `my_team_id` and current week.
- `GET /api/start-sit?team_id=99`: returns evaluation for team 99.
- Invalid team ID or invalid week returns 404 / 422 with clear message.
- Future week preview: returns `is_future_preview = True`, labels as uncommitted, does not mark moves as actionable.
- Missing database / missing snapshot returns clean structured degradation without 500 error.

- [ ] **Step 2: Run pytest to verify failures**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_api_start_sit" -v`
Expected: FAIL

- [ ] **Step 3: Implement `app/api/start_sit.py` and register in `main.py`**

Build endpoint handler conforming to the approved API specification. Register router cleanly.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_start_sit.py -k "test_api_start_sit" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/start_sit.py backend/app/main.py backend/tests/test_start_sit.py
git commit -m "feat(api): implement GET /api/start-sit endpoint"
```

---

### Task 6: Frontend Start/Sit Command Center Page

**Files:**
- Create: `fantasy-intel/frontend/app/start-sit/page.tsx`
- Create: `fantasy-intel/frontend/app/start-sit/start-sit.module.css`
- Modify: `fantasy-intel/frontend/app/layout.tsx`
- Test: `fantasy-intel/frontend/lib/start-sit.test.ts` (or page rendering tests)

**Interfaces:**
- Consumes: `api<StartSitResponse>('/api/start-sit?team_id=...&week=...')`.
- UI Sections:
  - Header: Team switcher, week toggle (current vs next week preview), sync timestamps.
  - Section A: Summary cards (Actual projected, Optimized projected, Gain, Coverage, Status badge).
  - Section B: Start/Sit Decisions (coordinated cards with Start/Sit badges, net gain, actionability tags).
  - Section C: Complete Recommended Lineup (slot order, current slot, lock badge, projection, opponent).
  - Section D: Injury & Uncertainty Warnings (accordion).
  - Section E: Game Locks breakdown (started vs remaining games).

- [ ] **Step 1: Write frontend unit tests for formatting & actionability helpers**

Write tests in `frontend/lib/start-sit.test.ts` testing helper functions for gain formatting, actionability badge tones, and slot labels.

- [ ] **Step 2: Run npm test to verify failure**

Run: `npm test` in `fantasy-intel/frontend`
Expected: FAIL

- [ ] **Step 3: Implement page, CSS module, and layout nav link**

Build `start-sit/page.tsx` with responsive design (mobile-first, 320px to 1440px), clean cards, accessible controls, and `layout.tsx` topbar link.

- [ ] **Step 4: Run npm test, typecheck, lint, and build**

Run in `fantasy-intel/frontend`:
- `npm test` -> PASS
- `npm run typecheck` -> PASS
- `npm run lint` -> PASS
- `npm run build` -> PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/app/start-sit/ frontend/app/layout.tsx frontend/lib/start-sit.test.ts
git commit -m "feat(frontend): build Weekly Start/Sit Command Center UI"
```

---

### Task 7: Documentation, Full Verification Suite & PR Creation

**Files:**
- Create: `fantasy-intel/docs/weekly_start_sit.md`

- [ ] **Step 1: Write user & developer documentation**

Document mathematical model, lock constraints, duplicate slot handling, projection sources, and API contract.

- [ ] **Step 2: Run full backend and frontend regression test suites**

Run in `fantasy-intel/backend`:
- `TZ=UTC .venv/bin/pytest tests/test_start_sit.py -v`
- `TZ=UTC .venv/bin/pytest --ignore=tests/test_projection_backtest.py`

Run in `fantasy-intel/frontend`:
- `npm test`
- `npm run lint`
- `npm run typecheck`
- `npm run build`

- [ ] **Step 3: Commit documentation and any refinements**

```bash
git add docs/weekly_start_sit.md
git commit -m "docs: add Weekly Start/Sit Command Center documentation"
```

- [ ] **Step 4: Push branch and create PR**

Push branch `gemini/weekly-start-sit` to origin and open a PR against `main`.

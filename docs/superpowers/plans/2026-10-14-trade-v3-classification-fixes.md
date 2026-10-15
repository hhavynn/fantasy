# Trade Classification Correctness Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two trade classification correctness bugs identified by Codex in PR #18: make fantasy-IR uncertainty classification symmetric between anchor and opponent, and make asset board positional PAR ranks and starter-caliber classification tie-aware.

**Architecture:** 
1. In `trade_practical.classify()`, evaluate fantasy-IR uncertainty symmetrically for any traded player (whether in `sends` or `receives`) whose activation/return is unconfirmed, classifying otherwise qualifying trades as `speculative`.
2. In `trade_assets.AssetBoard`, compute positional PAR ranks using standard competition-style ranking (`1 + count(strictly higher PAR)`) with floating-point tolerance `1e-9` on full-precision PAR values, granting `starter_caliber=True` to all tied players whose rank is at or within the starter threshold (`rank <= starters`), independent of ESPN player IDs.

**Tech Stack:** Python 3.12, FastAPI, DuckDB, pytest, uv

## Global Constraints

- Do not redesign Trade Discovery, change player projections, or alter the canonical trade evaluation engine.
- Preserve exact weekly lineup optimization, ESPN forward projections, regular-season and playoff deltas, injury-availability semantics, missing-versus-zero semantics, asset-package PAR methodology, and read-only behavior.
- Changes limited to `backend/app/analytics/trade_practical.py`, `backend/app/analytics/trade_assets.py`, `backend/tests/test_trade_v3.py`, and `docs/v3_trade_intelligence.md`.
- No new dependencies or database migrations.

---

### Task 1: Symmetrical Fantasy-IR Uncertainty Classification

**Files:**
- Modify: `fantasy-intel/backend/app/analytics/trade_practical.py:95-101`
- Test: `fantasy-intel/backend/tests/test_trade_v3.py`

**Interfaces:**
- `classify(row: dict, balance: dict, injuries: list[dict], phase: str, strictness: str) -> tuple[str, list[str]]`
- `injury_flags(state, pids) -> list[dict]` returns flag dicts containing `espn_player_id`, `status`, `warnings`, `earliest_eligible_week`, `return_confirmed`, `fantasy_ir_slot`.

- [ ] **Step 1: Write failing regression tests for fantasy-IR symmetry and availability states**

In `fantasy-intel/backend/tests/test_trade_v3.py`, add comprehensive tests covering:
1. `test_fantasy_ir_uncertainty_is_symmetric_between_anchor_and_opponent`:
   - Anchor sends a fantasy-IR player (unconfirmed return) -> must be "speculative".
   - Anchor receives a fantasy-IR player (unconfirmed return) -> must be "speculative".
   - Swapping manager identities preserves the "speculative" classification.
   - Canonical projected-point values and deltas remain unchanged.
2. `test_fantasy_ir_player_with_confirmed_return_is_concerns_not_speculative`:
   - A player on fantasy IR whose NFL return is confirmed (`return_confirmed=True`, status `RETURNED`) is classified as "concerns", not "speculative".
3. `test_nfl_ineligible_on_normal_bench_slot_is_concerns_not_speculative`:
   - A player who is NFL-ineligible for known weeks on a normal bench slot is classified as "concerns", not "speculative".
4. `test_unconfirmed_return_status_is_speculative`:
   - `ELIGIBLE_UNCONFIRMED` on bench -> "speculative".
   - `UNKNOWN` availability -> "speculative".
5. `test_fantasy_ir_trade_failing_mutual_improvement_is_excluded_one_sided`:
   - A trade involving a fantasy-IR player that yields non-positive lineup delta for either team is classified as "excluded_one_sided".
6. `test_fantasy_ir_trade_with_lopsided_package_is_excluded_lopsided`:
   - A trade involving a fantasy-IR player with a significant asset imbalance against the opponent is classified as "excluded_lopsided".

- [ ] **Step 2: Run test to verify it fails under existing code**

Run: `TZ=UTC uv run pytest fantasy-intel/backend/tests/test_trade_v3.py -k "test_fantasy_ir_uncertainty_is_symmetric_between_anchor_and_opponent"`
Expected: FAIL because anchor sending a fantasy-IR player produces `"concerns"` instead of `"speculative"`.

- [ ] **Step 3: Implement symmetrical fantasy-IR classification in `trade_practical.py`**

In `fantasy-intel/backend/app/analytics/trade_practical.py`, update `classify()`:
Replace:
```python
    receives = set(row["receives"])
    unsure = [f for f in injuries if f["status"] in (UNKNOWN, ELIGIBLE_UNCONFIRMED)
              or (f["fantasy_ir_slot"] and f["espn_player_id"] in receives)]
```
With:
```python
    unsure = [f for f in injuries if f["status"] in (UNKNOWN, ELIGIBLE_UNCONFIRMED)
              or (f["fantasy_ir_slot"] and not f.get("return_confirmed"))]
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `TZ=UTC uv run pytest fantasy-intel/backend/tests/test_trade_v3.py -k "fantasy_ir or availability"`
Expected: All pass.

- [ ] **Step 5: Commit changes**

```bash
git add fantasy-intel/backend/app/analytics/trade_practical.py fantasy-intel/backend/tests/test_trade_v3.py
git commit -m "fix(analytics): apply fantasy-IR uncertainty symmetrically to both sides of trades"
```

---

### Task 2: Tie-Aware Positional PAR Ranking and Starter-Caliber Classification

**Files:**
- Modify: `fantasy-intel/backend/app/analytics/trade_assets.py:88-138`
- Test: `fantasy-intel/backend/tests/test_trade_v3.py`

**Interfaces:**
- `AssetBoard(state, window: Window)`
  - `self.profiles[pid]["position_rank"]`: 1-based competition rank among players at that position with known PAR.
  - `self.profiles[pid]["position_rank_of"]`: total count of players at that position with known PAR.
  - `self.profiles[pid]["league_starters_at_position"]`: number of starting slots for that position across the league.
  - `self.profiles[pid]["starter_caliber"]`: True if `pos in self.starters and position_rank <= self.starters[pos]`.
  - `AssetBoard.package(mine_send, their_send)`: asset balance diagnostic label, counts of starter-caliber assets, ratios.

- [ ] **Step 1: Write failing regression tests for tie-aware ranking and starter-caliber classification**

In `fantasy-intel/backend/tests/test_trade_v3.py`, add comprehensive tests covering:
1. `test_tied_par_at_starter_cutoff_both_qualify_as_starter_caliber`:
   - Two players tied in PAR at the league starter cutoff both receive the same competition rank and `starter_caliber=True`.
2. `test_starter_caliber_independent_of_espn_player_ids`:
   - Swapping ESPN player IDs of tied players does not alter rank, starter-caliber status, or package label.
3. `test_tied_group_spanning_cutoff_and_entirely_above_or_below`:
   - Tie spanning multiple cutoff positions: all qualifying tied players become `starter_caliber=True`.
   - Tied group entirely above cutoff: all `starter_caliber=True`.
   - Tied group entirely below cutoff: all `starter_caliber=False`.
4. `test_par_difference_small_nonzero_not_tied`:
   - Minute difference (e.g. 0.0001) breaks the tie according to full precision, not rounded to equality.
5. `test_incomplete_projections_remain_unranked_and_insufficient_data`:
   - Missing projection week leaves `par` as None, rank as None, `starter_caliber=False`, and package as `insufficient_data`.
6. `test_package_labels_remain_consistent_with_tied_players`:
   - Verify `balanced`, `significant_they_give_more` / `significant_you_give_more`, `they_surrender_multiple_strong` / `you_surrender_multiple_strong`, and `replacement_level` packages.
   - Specifically verify that two tied strong assets surrendered both count toward `they_surrender_multiple_strong`, regardless of their ID ordering.

- [ ] **Step 2: Run test to verify it fails under existing code**

Run: `TZ=UTC uv run pytest fantasy-intel/backend/tests/test_trade_v3.py -k "test_tied_par_at_starter_cutoff_both_qualify_as_starter_caliber"`
Expected: FAIL because existing logic splits tied players across the cutoff into `True` and `False` based on ESPN ID.

- [ ] **Step 3: Implement tie-aware competition ranking and starter-caliber in `trade_assets.py`**

In `fantasy-intel/backend/app/analytics/trade_assets.py`:
1. In `_profile()`, include `"_raw_par": par if complete else None`.
2. In `AssetBoard.__init__()`:
   - Collect `(raw_par, pid)` per position for players with known PAR.
   - Cleanly remove `_raw_par` from `pr`.
   - Compute competition rank: `rank = 1 + sum(1 for other_raw in known_raws if other_raw > raw_par + 1e-9)`.
   - Assign:
     ```python
     pr["position_rank"] = rank
     pr["position_rank_of"] = len(items)
     pr["league_starters_at_position"] = self.starters.get(pos)
     pr["starter_caliber"] = pos in self.starters and rank <= self.starters[pos]
     ```
   - Sort `items` for deterministic presentation order: `items.sort(key=lambda x: (-x[0], x[1]))`.
3. Document the tie-handling interpretation in the `AssetBoard` docstring.

- [ ] **Step 4: Run tests to verify all pass**

Run: `TZ=UTC uv run pytest fantasy-intel/backend/tests/test_trade_v3.py -k "test_tied or test_starter_caliber"`
Expected: All pass.

- [ ] **Step 5: Commit changes**

```bash
git add fantasy-intel/backend/app/analytics/trade_assets.py fantasy-intel/backend/tests/test_trade_v3.py
git commit -m "fix(analytics): make positional PAR ranks and starter-caliber classification tie-aware"
```

---

### Task 3: Documentation and Full Suite Verification

**Files:**
- Modify: `fantasy-intel/docs/v3_trade_intelligence.md`
- Test: Full backend test suite & frontend checks

- [ ] **Step 1: Update documentation**

In `fantasy-intel/docs/v3_trade_intelligence.md`, document:
- Tie-aware competition ranking for positional PAR.
- When ties span the starter cutoff, all tied players qualify as starter-caliber.
- Separation of deterministic display ordering from value classification.

- [ ] **Step 2: Run full backend test suite**

Run: `TZ=UTC uv run pytest` in `fantasy-intel/backend`
Expected: 100% passing tests.

- [ ] **Step 3: Run frontend typecheck, lint, and build**

Run:
```bash
cd fantasy-intel/frontend && npm run lint && npm run typecheck && npm run build
```
Expected: All succeed without errors.

- [ ] **Step 4: Commit documentation updates**

```bash
git add fantasy-intel/docs/v3_trade_intelligence.md
git commit -m "docs(trade): document tie-aware starter cutoff and symmetrical fantasy-IR classification"
```

---

### Task 4: PR Creation and Review Gate

- [ ] **Step 1: Push branch to origin**

```bash
git push -u origin gemini/trade-v3-classification-fixes
```

- [ ] **Step 2: Create Pull Request**

Create PR against `main` using `gh pr create` with full description of both root causes, changes, and before/after verification.

- [ ] **Step 3: Verify GitHub CI and complete completion report**

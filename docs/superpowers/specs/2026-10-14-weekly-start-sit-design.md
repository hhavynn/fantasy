# Weekly Start/Sit Command Center Design Specification

**Date:** 2026-10-14  
**Author:** Gemini  
**Branch:** `gemini/weekly-start-sit`  
**Status:** Approved

---

## 1. Overview & Goals

The Weekly Start/Sit Command Center is a reliable, read-only decision tool that helps fantasy managers optimize their ESPN starting lineups for the current scoring period.

### Core Questions Answered
1. **Am I starting the best projected lineup with my current roster?** (Compares actual ESPN starters against the mathematically optimal lineup).
2. **Which bench players should I consider starting?** (Identifies entering bench players).
3. **Who would they replace, and in which roster slot?** (Identifies displaced starters and target slots).
4. **How many additional points does the optimized lineup project?** (Computes net projected points gain without double counting).
5. **Which decisions are affected by injury uncertainty, byes, or game locks?** (Distinguishes actionable, locked, and uncertain decisions).

---

## 2. Architecture & File Structure

Strict isolation is maintained to prevent any interference with concurrent trade/waiver work:
- **`backend/app/analytics/start_sit.py`**: Pure optimization logic, game-lock detection, slot-preserved constraint resolution, and decision attribution.
- **`backend/app/api/start_sit.py`**: Dedicated FastAPI router exposing `GET /api/start-sit`.
- **`backend/app/main.py`**: Registers `start_sit_router` on `/api`.
- **`backend/tests/test_start_sit.py`**: Comprehensive synthetic test suite (17+ distinct edge cases).
- **`frontend/app/start-sit/page.tsx`**: Next.js server/client component for the command center UI.
- **`frontend/app/start-sit/start-sit.module.css`**: Styling consistent with `fantasy-intel` design tokens.
- **`frontend/app/layout.tsx`**: Adds "Start/Sit" to top navigation.
- **`docs/weekly_start_sit.md`**: User-facing documentation explaining the mechanics and data sources.

---

## 3. Algorithmic Model: Constrained Re-Optimization with Game Locks

### 3.1 Game Lock Detection
For each rostered player:
- Query `nfl_schedules` for the current scoring period (`season`, `week`, `game_type='REG'`) where `home_team == pro_team` or `away_team == pro_team`.
- Convert `gameday` and `gametime` to UTC using `kickoff_utc(gameday, gametime)`:
  - **`LOCKED`**: Final score is present (`home_score` and `away_score` not null, excluding `0-0`), OR `kickoff <= now`.
  - **`UNLOCKED`**: `kickoff > now` and no final score.
  - **`UNKNOWN`**: Player has a pro team scheduled, but kickoff time is missing or unparseable.
  - **`BYE`**: Player's team has no scheduled game in `nfl_schedules` for this week.
  - **`TEAMLESS`**: Player has no NFL team (`pro_team is None`).

### 3.2 Constraint Formulation
Let $S_{total}$ be the list of required starting slots from ESPN `roster_slots` (e.g. `['QB', 'RB', 'RB', 'WR', 'WR', 'TE', 'FLEX', 'K', 'D/ST']`).

1. **Preserve Locked Starters:**
   - Any player currently starting in ESPN whose status is `LOCKED` cannot be benched or moved.
   - Their assigned slot is removed from $S_{total}$ to produce $S_{remaining}$.
2. **Filter Candidate Pool:**
   - Exclude locked starters (already assigned).
   - Exclude locked bench players (kicked off while on bench; cannot enter).
   - Exclude IR players (`on_ir == True`; cannot start without an active roster drop).
   - Exclude verified NFL-ineligible players (`week in unavailable_weeks`).
   - Exclude unprojected players (players without an ESPN projection cannot start).
3. **Solve Remaining Slots:**
   - Run pure `assign_lineup(candidate_pool, S_{remaining})` from `backend/app/analytics/lineup.py`.
   - Recommended Lineup = `[locked starters in fixed slots] + [optimized candidates in remaining slots]`.
4. **Benchmark Unconstrained Lineup:**
   - Also run `assign_lineup(all_eligible_roster_players, S_{total})` to report if an even better lineup was mathematically possible before kickoff locks occurred.

### 3.3 Start/Sit Decision Attribution
- Entering players: `E = {p | p in Recommended Starters and p not in Actual Starters}`
- Displaced players: `D = {p | p in Actual Starters and p not in Recommended Starters}`
- Net Lineup Gain: `recommended_lineup_total - actual_lineup_total` (exact single calculation; no double-counting or sum-of-swaps distortion).
- For each change:
  - Match entering player with displaced player.
  - Actionability:
    - `ACTIONABLE`: Neither entering nor displaced player is locked, neither kickoff is unknown, entering player is not on IR or NFL-ineligible.
    - `LOCKED`: Displaced player has kicked off OR entering player has kicked off on bench.
    - `UNCERTAIN`: Kickoff time missing or projection incomplete.
    - `IR_BLOCKED`: Player is currently on fantasy IR.

---

## 4. API Specification

### Endpoint: `GET /api/start-sit`

**Parameters:**
- `team_id: int` (Optional, defaults to `my_team_id` or first team)
- `week: int` (Optional, defaults to ESPN league `current_week`)

**Response:**
```json
{
  "league": { "id": 999, "name": "League Name", "season": 2026, "current_week": 7 },
  "team": { "id": 1, "name": "My Team" },
  "scoring_period": 7,
  "is_future_preview": false,
  "summary": {
    "actual_projected_total": 118.4,
    "recommended_projected_total": 124.6,
    "known_actual_total": 118.4,
    "known_recommended_total": 124.6,
    "projected_gain": 6.2,
    "projection_complete": true,
    "missing_projections": [],
    "starters_count": 9,
    "bench_count": 6,
    "ir_count": 1,
    "locked_players_count": 2,
    "status": "upgrade_available"
  },
  "recommended_lineup": [
    {
      "slot": "QB",
      "player_id": 1234,
      "name": "Patrick Mahomes",
      "position": "QB",
      "pro_team": "KC",
      "projected_points": 21.4,
      "is_flex": false,
      "is_currently_starting": true,
      "current_slot": "QB",
      "lock_status": "unlocked",
      "kickoff_utc": "2026-10-18T20:25:00+00:00",
      "injury_status": "ACTIVE",
      "availability_status": "available",
      "opponent": "SF"
    }
  ],
  "decisions": [
    {
      "id": "change-1",
      "action": "START",
      "actionability": "ACTIONABLE",
      "start_player": { "id": 5678, "name": "Brian Thomas Jr.", "position": "WR", "pro_team": "JAX", "projection": 14.2, "lock_status": "unlocked" },
      "bench_player": { "id": 9012, "name": "Tyler Lockett", "position": "WR", "pro_team": "SEA", "projection": 9.5, "lock_status": "unlocked" },
      "target_slot": "WR2",
      "projected_net_gain": 4.7,
      "explanation": "Starting Brian Thomas Jr. (14.2 pts) over Tyler Lockett (9.5 pts) in WR2 gains +4.7 projected points.",
      "warnings": []
    }
  ],
  "current_bench": [
    {
      "player_id": 9012,
      "name": "Tyler Lockett",
      "position": "WR",
      "pro_team": "SEA",
      "projected_points": 9.5,
      "lock_status": "unlocked",
      "kickoff_utc": "2026-10-18T20:05:00+00:00",
      "injury_status": "ACTIVE",
      "on_ir": false,
      "nfl_ineligible": false
    }
  ],
  "freshness": {
    "lineup_synced_at": "2026-10-14T20:00:00+00:00",
    "projection_synced_at": "2026-10-14T20:00:00+00:00",
    "nfl_schedule_synced_at": "2026-10-14T20:00:00+00:00"
  }
}
```

---

## 5. Frontend UI Design (`frontend/app/start-sit/page.tsx`)

### 5.1 Controls & Navigation
- League team selector dropdown (defaults to user team).
- Scoring period selector: Current week vs Next-week informational preview.

### 5.2 Section Layout
1. **Section A — Weekly Lineup Summary:**
   - Cards showing Actual Projected Total, Optimized Total, Potential Gain, Status badge (`Already Optimal`, `Upgrade Available`, `Decisions Locked`), and Freshness pill.
2. **Section C — Recommended Start/Sit Changes:**
   - Prominently positioned below summary.
   - Action cards showing Start Player, Bench Player, Target Slot, Projected Net Gain, Actionability badge, and narrative explanation.
3. **Section B — Complete Recommended Lineup:**
   - Strict slot order: QB, RB, WR, TE, FLEX, K, D/ST.
   - Shows current slot, projected points, opponent, lock status, and whether the player is currently starting vs on the bench.
4. **Section D — Injury & Uncertainty Warnings:**
   - Prominently highlights injury tags (`QUESTIONABLE`, `OUT`, `IR`) and NFL reserve stints.
5. **Section E — Game Locks & Schedule Status:**
   - Clear breakdown of started games, upcoming games, and any missing kickoff times.

---

## 6. Testing & Edge Cases

The synthetic test suite (`backend/tests/test_start_sit.py`) verifies:
1. Normal weekly optimization with standard upgrades.
2. Correct FLEX selection among multi-position pools.
3. Complex multi-player FLEX cascades without double counting.
4. Already-optimal lineups (0 moves, 0.0 gain).
5. Bye week player excluded from lineup.
6. Injured player with positive projection reported with injury warnings.
7. Verified NFL-ineligible player excluded.
8. Fantasy IR player excluded and tagged `IR_BLOCKED`.
9. Missing projections set `projected_gain = None`.
10. True 0.0 projection distinguished from missing.
11. Locked starter remains in starting slot.
12. Locked bench player cannot enter lineup.
13. Partially started week optimizes only remaining slots.
14. Unknown kickoff time flags decision as `UNCERTAIN`.
15. Missing roster snapshot handled gracefully.
16. Deterministic tie-breaking on player ID.
17. Full mathematical equivalence to `assign_lineup` when no locks exist.

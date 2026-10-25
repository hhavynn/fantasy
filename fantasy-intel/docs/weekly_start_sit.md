# Weekly Start/Sit Command Center

Code: `backend/app/analytics/start_sit.py`, `backend/app/api/start_sit.py`, `backend/tests/test_start_sit.py`,
`frontend/app/start-sit/page.tsx`, `frontend/app/start-sit/StartSitControls.tsx`, `frontend/app/start-sit/types.ts`, `frontend/lib/start-sit-helpers.ts`.

## Core Idea

The Weekly Start/Sit Command Center is an actionable, read-only decision tool that compares a fantasy manager's **actual submitted ESPN starting lineup** against the **highest-projected lineup achievable under strict real-world constraints**.

It directly answers five weekly questions:
1. **Am I starting the best projected lineup with my current roster?**
2. **Which bench players should I consider starting?**
3. **Who would they replace, and in which roster slot?**
4. **How many additional points does the optimized lineup project?**
5. **Which decisions are affected by kickoff locks, injuries, byes, or missing projections?**

---

## Architecture: Three Lineup Benchmarks

Rather than relying on abstract rankings or pairwise comparisons, the system evaluates three distinct lineups for the requested week:

### 1. Actual ESPN Lineup
- Loaded directly from `lineup_snapshots` for the requested scoring period (`team_id`, `scoring_period`).
- Preserves the manager's exact submitted starters, bench assignments, and fantasy IR placements.
- Captures slot multiplicity (`RB_1`, `RB_2`, `WR_1`, `WR_2`, `FLEX_1`) and the exact snapshot synchronization timestamp.
- Never inferred or recalculated; reflects the exact state recorded in ESPN.

### 2. Lock-Constrained Recommended Lineup (Primary Decision Lineup)
- Preserves all assignments for players whose games have already kicked off or are confirmed locked.
- Optimizes only the remaining changeable slots using available, unlocked bench players.
- Excludes fantasy IR players and confirmed NFL-ineligible players from entering starting slots.
- Solves FLEX eligibility using the exact canonical solver in `backend/app/analytics/lineup.py`.

### 3. Unconstrained Mathematical Optimum (Theoretical Benchmark)
- Solves the theoretical maximum lineup achievable by the roster if all players were unlocked and freely swappable.
- Serves as an informational benchmark to quantify "lock drag" (points lost because superior bench players kicked off before being inserted).

---

## Conservative Game-Lock Detection

Recommendations never advise benching or starting players whose games have kicked off.

Each player's lock status is categorized into one of five verified states:

| Status | Definition | Rules & Invariants |
| :--- | :--- | :--- |
| `LOCKED` | Game has kicked off | `kickoff_utc <= now` OR confirmed final score recorded (`home_score` / `away_score`). |
| `UNLOCKED` | Future kickoff | `kickoff_utc > now` with no contradictory completed game evidence. |
| `BYE` | Confirmed NFL bye week | Player's NFL team has no scheduled game in `nfl_schedules` **AND** schedule coverage is verified (at least 28 teams scheduled across the league). |
| `TEAMLESS` | NFL Free Agent | Player has no assigned NFL team. |
| `UNKNOWN` | Unverified schedule evidence | Missing schedule row, ambiguous team abbreviation, or kickoff timestamp missing. Never treated as executable. |

### Conservative Lock Invariants
- **No retroaction:** If an actual starter's game has kicked off, their slot assignment is frozen in the recommended lineup.
- **No post-kickoff promotion:** If a bench player's game has kicked off, they can never be promoted into a starting slot.
- **No false byes:** Missing schedule rows are never treated as byes unless the week's schedule coverage is verified.
- **Injected evaluation time:** All lock calculations accept a timezone-aware `now` parameter for deterministic testing and replay.

---

## Net Gain Attribution & Slot Cascade Optimization

### Non-Additive Net Gain
The headline projected gain is always:
$$\text{Projected Net Gain} = \text{Recommended Projected Total} - \text{Actual Projected Total}$$
Pairwise slot swaps are **never summed** together, as FLEX dependencies and multiple moves share opportunity cost. If any starter or promoted player lacks an ESPN projection, the comparison is marked **incomplete**, and no numerical net gain is claimed.

### Actionability Categorization
Each decision item is assigned an explicit actionability tier:
- `ACTIONABLE`: Valid move; all involved players are unlocked with verified kickoffs.
- `LOCKED`: One or more involved players have already kicked off; move cannot be legally executed in ESPN.
- `UNCERTAIN`: Kickoff timing, schedule coverage, or projection data is missing or unverified.
- `IR_BLOCKED`: The promoted player is on fantasy IR and cannot legally start without a separate transaction.
- `NO_CHANGE`: Submitted lineup is already optimal.

### FLEX Cascade Handling
When a bench player's optimal entry causes positional shuffling (e.g., bench WR enters WR2, bumping former WR2 to FLEX, displacing former FLEX RB to bench), the system outputs a single coordinated `COORDINATED_SWAP` card rather than deceptive, independent 1-for-1 swaps.

---

## Future Week Preview Semantics

When viewing a future scoring period (`week > current_week`):
- Clearly labeled as an **informational projection**, not a submitted ESPN lineup.
- Uses current roster assets projected under the future week's matchup and bye schedules.
- Prevents false claims of actionability until the current scoring period has finalized and waivers/lineups open for the next week.

---

## API Specification

### `GET /api/start-sit`

**Parameters:**
- `team_id` (optional, integer): Defaults to the authenticated user's fantasy team (`is_mine = true`). With no such team (a multi-league viewer with no saved team and no `?team_id=`) the response is 404 "No team is marked as yours", never another team's lineup (#348).
- `week` (optional, integer): Scoring period / NFL week. Defaults to `league.current_week`.

**Response Payload Schema:**
```json
{
  "league": {
    "id": 123456,
    "name": "League Name",
    "season": 2026,
    "current_week": 7,
    "final_scoring_period": 17
  },
  "team": {
    "id": 1,
    "name": "My Team",
    "abbrev": "MINE",
    "is_mine": true
  },
  "teams": [
    { "id": 1, "name": "My Team", "abbrev": "MINE", "is_mine": true },
    { "id": 2, "name": "Opponent", "abbrev": "OPP", "is_mine": false }
  ],
  "scoring_period": 7,
  "is_future_preview": false,
  "projection_source": "ESPN",
  "summary": {
    "actual_projected_total": 112.4,
    "recommended_projected_total": 116.8,
    "known_actual_total": 112.4,
    "known_recommended_total": 116.8,
    "unconstrained_total": 116.8,
    "known_unconstrained_total": 116.8,
    "projected_gain": 4.4,
    "projection_complete": true,
    "missing_projections": [],
    "starters_count": 9,
    "bench_count": 6,
    "ir_count": 1,
    "locked_players_count": 2,
    "all_locked": false,
    "status": "upgrade_available"
  },
  "recommended_lineup": [ ... ],
  "decisions": [
    {
      "id": "swap_101_102",
      "action": "SWAP",
      "actionability": "ACTIONABLE",
      "start_players": [ { "id": 101, "name": "Bench Star", "position": "WR", "pro_team": "MIN", "projection": 14.5, "lock_status": "UNLOCKED" } ],
      "bench_players": [ { "id": 102, "name": "Current Starter", "position": "WR", "pro_team": "CHI", "projection": 10.1, "lock_status": "UNLOCKED" } ],
      "target_slot": "WR",
      "projected_net_gain": 4.4,
      "explanation": "Start Bench Star (14.5 pts) over Current Starter (10.1 pts) in WR for a +4.4 pt projected improvement.",
      "warnings": []
    }
  ],
  "current_bench": [ ... ],
  "injury_warnings": [ ... ],
  "schedule_warnings": [ ... ],
  "freshness": {
    "lineup_synced_at": "2026-10-14T20:00:00Z",
    "projection_synced_at": "2026-10-14T19:30:00Z",
    "nfl_schedule_synced_at": "2026-10-14T18:00:00Z"
  },
  "limitations": [
    "Lock detection uses conservative NFL kickoff times and final scores from stored schedule data; exact ESPN transaction/lock acceptance cannot be guaranteed.",
    "Projections are unmodified weekly ESPN projections (ESPN league scoring); live actual points are not blended into pregame projections.",
    "Future preview is an informational projection on current roster assets and does not represent a submitted ESPN lineup."
  ]
}
```

---

## Verification & Testing

The module is verified by:
- `backend/tests/test_start_sit.py`: 14 unit and integration tests covering:
  - Kickoff lock boundary conditions (before, during, and after kickoff; final scores).
  - Schedule coverage verification (avoiding false byes on incomplete schedule data).
  - Multiplicity preservation across slot instances (`RB_1`, `RB_2`, `WR_1`, `WR_2`).
  - Strict preservation of locked starters in partially started weeks.
  - Prevention of locked bench players entering the lineup.
  - Coordinated FLEX cascades without double counting.
  - Future week preview behavior without data fabrication.
  - End-to-end FastAPI endpoint response contract.
- `frontend/lib/start-sit.test.ts`: Unit tests verifying formatters, actionability tones, and kickoff status formatting.
- `npm run typecheck`, `npm run lint`, and `npm run build`: Production Next.js compilation verification.

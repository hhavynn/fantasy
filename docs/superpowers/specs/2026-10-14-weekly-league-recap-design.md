# Weekly League Recap Design Specification

**Date:** 2026-10-14  
**Feature Branch:** `feature/weekly-league-recap`  
**Status:** Approved  
**Author:** Gemini  

---

## 1. Executive Summary & Purpose

Build a compact, read-only **Weekly League Recap** feature that makes the fantasy league's completed weeks interesting to explore. The feature operates exclusively on verified completed historical matchups and league records from DuckDB, completely isolated from concurrent trade intelligence and waiver systems.

---

## 2. Source-Data Audit & Verification

| Requirement | DuckDB Stored Source | Verification Status & Findings |
|---|---|---|
| Matchup results & scores | `matchups` (`matchup_period`, `home_team_id`, `away_team_id`, `home_points`, `away_points`, `winner`) | Complete for finished weeks. Only rows where `winner IN ('HOME', 'AWAY', 'TIE')` are treated as final. Matchups with `UNDECIDED` or in-progress status are strictly ignored. |
| League rules & period mapping | `leagues` (`settings_raw.scheduleSettings.matchupPeriods`, `reg_season_matchups`, `final_scoring_period`) | Multi-week matchups (e.g. 2-week playoff rounds) are supported via `period_weeks(schedule_settings, period)`. |
| Standout player performances | `espn_player_weekly_points` (`actual_points`, `scoring_period`), `fantasy_players` (`full_name`, `position`, `pro_team`), `lineup_snapshots` (`is_starter`, `team_id`) | Uses exact league-scored `actual_points` from ESPN (statSplitTypeId 1, statSourceId 0). Joined with `lineup_snapshots` to attribute rostered team and Starter vs. Bench status. Unrostered players are marked as Free Agents. Missing points remain `null`, never converted to 0. |
| League activity & transactions | `transactions` (`scoring_period`, `type`, `status`, `process_date`, `bid_amount`), `transaction_items` (`item_type`, `espn_player_id`, `from_team_id`, `to_team_id`) | Attributed by `scoring_period` and `status = 'EXECUTED'`. Distinguishes `ADD`, `DROP`, and `TRADE`. Real timestamps displayed when present; missing timestamps left `null`. |
| Historical standings reconstruction | `matchups` where `matchup_period <= selected_week` and `winner IN ('HOME', 'AWAY', 'TIE')` | Standings are calculated strictly from completed games up to the selected week. No future-data leakage. Verified to match official records exactly when accumulated. |
| Pregame projections / Upset award | `espn_projection_history` / `espn_player_weekly_points.projected_points` | **Audit Finding:** Projections in the database have post-hoc sync timestamps from 2026-10-14, meaning authentic pregame snapshot projections are unavailable for historical completed weeks. Per user requirement ("Never use projections fetched after the games to reconstruct pregame expectations"), the "Biggest Upset" award is **omitted** and replaced with an explicit disclosure. |

---

## 3. Architecture & File Layout

Work is strictly isolated to the following dedicated files:

- `backend/app/analytics/weekly_recap.py`: Pure-function data queries and analytics models.
- `backend/app/api/weekly_recap.py`: FastAPI router handling `GET /api/weekly-recap`.
- `backend/tests/test_weekly_recap.py`: Synthetic pytest suite covering edge cases (ties, multiweek, empty weeks, missing player points, etc.).
- `frontend/app/weekly-recap/page.tsx`: Next.js frontend page with week selector, awards, scoreboard, standout players, transactions, and standings.
- `frontend/lib/weekly-recap.ts`: TypeScript types and API client helper for weekly recap data.
- `docs/weekly_recap.md`: End-user and developer documentation.
- `backend/app/main.py`: Single line router registration (`app.include_router(weekly_recap_router)`).
- `frontend/app/layout.tsx`: Single navigation link added to top navigation.

No modifications to trade evaluation, trade discovery, waiver valuation, or player scoring models.

---

## 4. API Specification

### Endpoint: `GET /api/weekly-recap`
**Query Parameters:**
- `week` (optional integer): Matchup period to retrieve. If omitted, defaults to the latest completed week (`max(matchup_period)` where complete).

### Response Schema (`WeeklyRecapResponse`)
```json
{
  "season": 2026,
  "league_id": 854268537,
  "league_name": "Los Subies",
  "selected_week": 4,
  "available_weeks": [1, 2, 3, 4, 5],
  "is_completed_week": true,
  "scoring_periods": [4],
  "matchups": [
    {
      "matchup_id": 1,
      "matchup_period": 4,
      "home_team": { "team_id": 4, "name": "JOEEE BRRRR", "abbrev": "Tune", "points": 131.46 },
      "away_team": { "team_id": 6, "name": "Jonnie Blue", "abbrev": "JNBL", "points": 92.34 },
      "winner": "HOME",
      "margin": 39.12,
      "is_tie": false
    }
  ],
  "awards": {
    "highest_score": {
      "award_name": "Highest Scorer",
      "winners": [{ "team_id": 2, "name": "To Infinity and Bijan", "abbrev": "GOON", "points": 174.4 }],
      "is_tied": false,
      "description": "Top scoring team of the week"
    },
    "biggest_blowout": {
      "award_name": "Biggest Blowout",
      "winners": [{ "matchup_id": 2, "winner_name": "To Infinity and Bijan", "loser_name": "los changitos", "margin": 68.7, "score_summary": "174.40 - 105.70" }],
      "is_tied": false,
      "description": "Largest margin of victory"
    },
    "closest_matchup": {
      "award_name": "Closest Matchup",
      "winners": [{ "matchup_id": 3, "winner_name": "Tortas", "loser_name": "Njigbas in Paris", "margin": 1.3, "score_summary": "117.46 - 116.16" }],
      "is_tied": false,
      "description": "Decided by the narrowest margin"
    },
    "highest_scoring_loser": {
      "award_name": "Tough Luck",
      "winners": [{ "team_id": 5, "name": "Njigbas in Paris", "abbrev": "NPS", "points": 116.16 }],
      "is_tied": false,
      "description": "Highest score in a losing effort"
    },
    "biggest_upset": {
      "award_name": "Biggest Upset",
      "winners": [],
      "is_tied": false,
      "omitted_reason": "Authentic pregame projected matchup scores are unavailable. Stored projection timestamps indicate post-hoc syncs; to protect data authenticity, this award is omitted."
    }
  },
  "standout_players": [
    {
      "espn_player_id": 4431611,
      "full_name": "Caleb Williams",
      "position": "QB",
      "pro_team": "CHI",
      "points": 37.26,
      "fantasy_team_id": 10,
      "fantasy_team_name": "Carted Off Field",
      "lineup_status": "BENCH"
    }
  ],
  "league_activity": {
    "total_moves": 14,
    "adds": [
      {
        "player_id": 4036378,
        "player_name": "Jordan Love",
        "position": "QB",
        "team_id": 11,
        "team_name": "Hasbi’s ballers",
        "type": "WAIVER",
        "bid_amount": 0.0,
        "processed_at": "2026-10-01T07:04:32.208000"
      }
    ],
    "drops": [...],
    "trades": [
      {
        "team_a_name": "Jonnie Blue",
        "team_b_name": "jc_folk",
        "players_to_a": ["Jared Goff"],
        "players_to_b": ["RJ Harvey"],
        "processed_at": "2026-10-02T06:26:36.623000"
      }
    ]
  },
  "standings": [
    {
      "rank": 1,
      "team_id": 9,
      "team_name": "Tortas",
      "wins": 4,
      "losses": 1,
      "ties": 0,
      "win_pct": 0.8,
      "points_for": 657.26,
      "points_against": 544.34,
      "weekly_result": "W"
    }
  ]
}
```

---

## 5. Analytics Calculation Rules & Safeguards

1. **Deterministic Award Selection & Ties:**
   - On ties, all co-winners are preserved and returned in `winners`, sorted deterministically by primary ID (`team_id` or `matchup_id`). `is_tied: true` is flagged.
2. **Missing Points vs. Real Zeros:**
   - Real zero points (e.g. kicker 0.0 or bye 0.0) is 0.0. Missing points from unrecorded stats remain `null`.
3. **Multi-week Matchup Scoring Periods:**
   - Uses `period_weeks(schedule_settings, period)`. If a matchup period spans multiple weeks, transactions and player performances from all constituent weeks are attributed to that recap.
4. **Historical Standings Order:**
   - Calculated strictly from weeks $\le W$.
   - Primary: Win percentage `(wins + 0.5 * ties) / total_games`.
   - Secondary: `points_for` descending.
   - Tertiary: `team_id` ascending.
5. **No Speculative Rankings or Projections:**
   - No arbitrary power rankings, no post-hoc projection reconstructions, and no synthetic win probabilities.

---

## 6. Frontend Presentation

- Accessible at `/weekly-recap`.
- Top navigation link between `Schedule` and `Playoff race`.
- Clean card-based design with monospace numerical alignment.
- Responsive layout with desktop 2-column matchup cards and mobile stacking.
- Tabbed / categorized activity breakdown for adds, drops, and trades.
- Standings table with weekly result badges (`W`, `L`, `T`).
- Comprehensive empty state when no completed weeks exist.

---

## 7. Testing Plan

Synthetic unit tests in `backend/tests/test_weekly_recap.py` utilizing in-memory DuckDB connections covering:
- Normal completed week
- Tied matchup (winner = 'TIE', home_points == away_points)
- Closest matchup edge cases
- No completed matchups (empty/early season)
- Missing individual player points (null handling)
- Multiweek matchup period mapping
- Incomplete transaction data (null process_date preserved)
- Actual league scoring correctness
- Historical standings accuracy & no future leakage
- Deterministic award selection when tied
- Verification that trade and waiver logic remain untouched
- Full pytest suite, Next.js lint, and production build verification

# M2 — This Week: weekly command center + matchup page

Branch: `polish/m2-this-week` · Ports: backend 8102 (your own, you add an endpoint) · frontend 3102
Report: `/private/tmp/claude-501/-Users-havyn-Documents-CS-Fantasy/f2409be7-3d20-4090-a192-bdc2f1aaee45/scratchpad/reports/M2-report.md` (outside the repo; never commit it)

## Goal

Answer the first question every manager has when they open the site: **"What's my matchup
this week and what should I do about it?"** Add a matchup page and turn Overview into a
weekly command center. Built entirely on the M1 kit (`components/ui`, `components/charts`).

## 1. Backend: `GET /api/matchup?week=<int optional>&team_id=<int optional>`

New module `backend/app/api/matchup.py` (router registered in `app/main.py`), analytics
helper in `backend/app/analytics/matchup.py` if there is any logic beyond row shaping.
Defaults: the current league week and the user's team (follow how `routes.py` resolves
`_league()` and the "my team" id; reuse helpers, don't duplicate).

Response (document it in a docstring; frontend type in `lib/types.ts` or `lib/matchup-types.ts`):

```
{
  league: {league_id, season, name, current_week},
  week, phase ("regular" | "playoff" | null), matchup_id, is_bye_week (no opponent),
  weeks_available: [int],                 // weeks that have a matchup row for this team
  me:   TeamSide, opponent: TeamSide | null,
  status: "upcoming" | "in_progress" | "final" | "unknown",   // from data you actually have; explain the rule in the docstring
  notes: [string]                          // data caveats (projection incomplete, etc.)
}
TeamSide = {
  team_id, name, abbrev?, record: {wins, losses, ties}, standing?,
  actual_points: number|null,              // matchups.home/away_points when status != upcoming
  projected_points: number|null,           // team_week_lineup_projections.projected_lineup_points (NULL unless complete — keep NULL)
  known_points: number|null,               // known_lineup_points
  projection_complete: boolean,
  lineup: [{slot, player_id, name, position, pro_team, points (projected), is_flex,
            injury_status, on_bye}],      // from the projection `assignment` JSON (the optimal lineup)
  bench:  [{player_id, name, position, pro_team, points, injury_status}],
  actual_lineup?: [...]                    // the lineup actually set (lineup_snapshots) when available; label which is which
}
```

Rules: use `team_week_lineup_projections` for projected optimal lineups (it already handles
completeness; respect `projection_complete`, never fill NULLs), `matchups` for pairing and
scores, `fantasy_teams` for names/records, `fantasy_players` (or what the roster query in
`routes.py` uses) for injury status / pro team / bye. If the week has no matchup for the
team (bye or out of range) return `opponent: null` with a note. 404 for unknown team_id,
422 for week outside `weeks_available` range.

Tests: `backend/tests/test_matchup_api.py` using the existing synthetic fixtures
(`tests/synthetic_*.py`, see how `test_team_lineups.py`/`test_playoff_race.py` build a DB):
default week/team, explicit week, bye week, incomplete projection keeps `projected_points`
null and adds a note, unknown team 404.

## 2. Frontend: `/matchup` page (`app/matchup/page.tsx`)

- Add to nav (`lib/navigation.ts`) under "My team" after Overview: `{ href: "/matchup", label: "Matchup" }`;
  update `lib/navigation.test.ts` accordingly.
- Header: "Week N matchup" with phase; week switcher (`Tabs` or prev/next links via
  `?week=`), team switcher not needed (always you) but support `?team_id=` passthrough.
- **Scoreboard hero**: you vs opponent, side by side: team names, records, big projected
  totals (display font) and the projected margin (`Delta`), actual points when the week has
  started/finished (clearly labeled "Actual" vs "Projected"), and a horizontal
  two-tone bar showing projected share. If a projection is incomplete, show the known total
  with "incomplete" and the reason; never present it as complete.
- **Head-to-head lineup table**: rows = slots (QB, RB, RB, WR, …, FLEX, D/ST, K) aligned
  across both teams: `my player · proj | slot | proj · their player`. Highlight the higher
  projection per row subtly; injury badges; bye badges. Phones: stack as two lists (my
  lineup, their lineup) instead of a 5-column table.
- **Bench** for both sides (collapsible on phones).
- **Notes/caveats** as a `Notice` when present. Method section explaining that projections
  are ESPN projections run through the optimal-lineup solver.
- Empty states: bye week, no projections yet, backend error (`BackendProblem`).

## 3. Overview → weekly command center (`app/page.tsx`)

Keep it one scroll, decision-first. Fetch in parallel with `Promise.all` (overview,
matchup, start-sit, waivers) and degrade gracefully per section if one call fails (each
section shows its own small notice; the page never fails entirely because one endpoint did).

Order:
1. PageHeader (team name, league meta) + StatStrip (record/standing, PF rank, PA rank,
   FAAB/waiver priority, injuries) — as M1 built.
2. **This week** band (grid-main): left = compact matchup card (both teams, projected totals,
   margin, link "Full matchup →"); right = **Action items** list, prioritized, each item one
   line with a tone icon, a short reason, and a link to the page that has the evidence:
   - Start/Sit: lineup swaps the Start/Sit endpoint recommends for this week with their
     projected gain (read `/api/start-sit`; inspect its response and `app/start-sit/page.tsx`
     for the field names; only list moves the backend marks actionable).
   - Injured or bye starters in the current lineup.
   - Top waiver recommendation(s) from `/api/waivers` (recommendation CLAIM/BUY or whatever
     the top tier is — inspect the data) with suggested drop when present.
   - If nothing is actionable: a calm "Lineup is set" empty state.
3. Roster section (as M1), needs, injuries (only if any), standings, recent transactions.
   It's fine to move standings/transactions below the roster in a two-column layout.

Don't compute new analytics in the frontend: the action list only selects, filters and
orders what the backend already returns.

## Acceptance

- Global checks pass (frontend + backend).
- `/matchup` renders for the current week, a past week, and a future week (check
  `weeks_available`), and a bye/invalid week shows the right empty state, at 1440 and 375.
- Overview renders the This-week band with real data; killing one endpoint (e.g. request a
  non-existent path in a temporary local edit, not committed) degrades only that section.
- No inline styles in the new/rewritten files except chart/bar geometry.

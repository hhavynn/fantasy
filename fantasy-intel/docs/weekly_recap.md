# Weekly League Recap

A read-only look back at one completed matchup period: awards, the scoreboard, team scores, the standings as
they stood after that week, standout player scores and the league's adds, drops and trades. Design spec:
`docs/superpowers/specs/2026-10-14-weekly-league-recap-design.md` (repo root).

- Page: `/weekly-recap` (`?week=N` picks a matchup period; `players=top|bench|unrostered` and
  `moves=adds|drops|trades` pick the tabs). Nav: League → Weekly recap, also in the command palette.
- API: `GET /api/weekly-recap?week=N` (default: the latest period with a completed matchup). 404 for a period
  with no completed matchup, 503 before the first ESPN sync. No writes, syncs or ESPN calls.
- Code: `backend/app/analytics/weekly_recap.py` (loader `load_recap_data`, pure `build_weekly_recap`),
  `backend/app/api/weekly_recap.py`, `frontend/app/weekly-recap/`, `frontend/lib/weekly-recap.ts`.

## Rules

- **Completed matchups only.** A matchup counts when ESPN's `winner` is `HOME`, `AWAY` or `TIE`. `UNDECIDED`
  matchups never count, even with live points. A period with some final games shows those games and says how
  many are still open (`is_completed_week: false`, `notes.pending_matchups`).
- **Multi-week periods.** Scoring weeks come from `playoff_race.period_weeks` (ESPN
  `scheduleSettings.matchupPeriods`). Player points and transactions from every week in the period count.
- **Awards** compare matchup-period totals: highest scorer, biggest blowout (ties excluded), closest matchup
  (ties included, margin 0) and tough luck (highest losing score). Values are compared to the hundredth of a
  point; equal values share the award, with co-winners sorted by team or matchup id (`is_tied: true`).
- **No Biggest Upset.** The stored projections for past weeks were synced after the games, so there is no
  genuine pregame expectation to compare with. The award is returned empty with `omitted_reason`.
- **Player points** are ESPN's league-scored `actual_points`. A missing value stays unknown (never 0) and the
  player is left out; `notes.rostered_without_points` counts rostered players affected. In a multi-week period
  a total needs every week (`notes.partial_period_players` counts the rest). Lineup status (started, bench,
  IR, unrostered) comes from that week's roster snapshot; a player whose team or status changed inside a
  multi-week period is `MIXED`.
- **Standings** count completed regular-season matchups through the selected period only, so later results
  never leak in. Order: win percentage, then points for, then team id (a display order, not ESPN's playoff
  tiebreak). `rank_change` compares with the previous completed regular-season week. In playoff weeks the
  table is the final regular-season one. On the latest regular-season week the rebuilt records are compared
  with ESPN's (`notes.espn_record_check`).
- **Activity** lists executed adds and drops (waiver, free agent, release, trade clean-up drops) by the
  transaction's scoring period; failed and cancelled claims are left out. Trades come from
  `manager_loader.load_trades` (activity feed plus transactions, deduplicated). A trade takes its ESPN
  transaction's scoring period; a trade seen only in the activity feed is placed in the one period whose
  executed transactions span its date, otherwise it is counted in `unplaced_trades` and not shown.
  Timestamps are ESPN's processing time, or the request time for free-agent moves (which execute at once);
  a move with neither stays `null`.

## Tests

`backend/tests/test_weekly_recap.py` (synthetic league): scoreboard and awards by hand, ties and co-winners,
float-noise margins, partly final and empty seasons, final games without scores, missing vs. zero player
points, two-week periods (points, moves, mixed teams), standings without future leakage and playoff weeks, the
ESPN record check, trade placement, and read-only API behaviour. `frontend/lib/weekly-recap.test.ts` covers
the URL state and view helpers.

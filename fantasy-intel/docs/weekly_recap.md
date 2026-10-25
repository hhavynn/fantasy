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

## Decision feedback ("Your decisions", issue #194)

`GET /api/weekly-recap/feedback?week=&source=` (also under `/v2/leagues/{lg}/api/...`) grades, for the viewer's
**own team only**, the start/sit calls implied by inputs that were archived **before kickoff**
(`backend/app/analytics/decision_feedback.py`). The recap page streams it in under the awards.

**Archive (as-of time).** Nothing is rebuilt from current data:

| Input | Archive | Cutoff |
|---|---|---|
| Roster | `roster_history` daily snapshots (a snapshot taken before a week's first kickoff is locked by the sync) | latest snapshot synced strictly after week W-1's last kickoff and strictly before week W's first kickoff; IR-slot players are not options |
| ESPN forecast | `espn_projection_history` daily snapshots (pregame-locked by the sync) | latest `synced_at` strictly before the player's own kickoff |
| Ours forecast | sealed, append-only production runs via `production_forecasts.pregame_forecasts` (content hash, approved artifact, artifact fitted before the week's first kickoff) | run `created_at` strictly before the player's own kickoff; production model, else the labeled fallback for a week without one |
| Kickoff | `nfl_schedules` | earliest known kickoff among the player's current team, his nflverse weekly roster team and the forecast row's team (a team change only makes a forecast less eligible); unknown = never pregame |

Each call carries its inputs (snapshot id, capture time, kickoff, model version, scoring version, content hash),
and each week its roster snapshot id and capture time. A week without a qualifying roster snapshot or forecast says
"No archived recommendations for this week" and grades nothing. Today's hosted and laptop data have no week whose
roster was archived before kickoff until syncs run before the week's first game; the feedback starts there.

**Grades** (exact definitions in the response's `definitions`): the archived lineup maximises the archived
forecasts over the decision set in the slots the source covers (Ours: QB/RB/WR/TE slots only). For each filled slot
the call is "start x over y", y the best-forecast benched player eligible for the slot. margin = actual(x) -
actual(y); correct > 0, missed < 0, push = 0; hit rate = correct / (correct + missed). Opportunity cost bounds:
lower = max(0, actual(y) - actual(x)), upper = max(0, best eligible benched actual - actual(x)), unknown when an
eligible benched player has no recorded points. A call with a forecast gap under 3 points is a close call. Lineup
grades (archived, best, yours) only when every decision-set player has recorded points. Missing data is never zero:
players and calls without an archived input or an outcome are excluded and counted by reason. No probabilities,
confidence or letter grades.

**Authorization.** The route takes no team: it reads `fantasy_teams.is_mine` (the signed-in viewer's saved team on
/v2). Any `team_id` gets the same 404 as an unknown league, because on /v2 a URL team becomes the perspective of a
viewer without a saved team. Season-to-date numbers aggregate that one team's weeks only.

**Not yet covered (follow-up).** Waiver recommendations are recomputed in place (`waiver_recommendations`,
`team_waiver_recommendations` keep only the latest run, without a week), so no as-of-time waiver archive exists and
waiver candidates are not graded. Kicker and D/ST runs of Ours are not read yet.

## Tests

`backend/tests/test_weekly_recap.py` (synthetic league): scoreboard and awards by hand, ties and co-winners,
float-noise margins, partly final and empty seasons, final games without scores, missing vs. zero player
points, two-week periods (points, moves, mixed teams), standings without future leakage and playoff weeks, the
ESPN record check, trade placement, and read-only API behaviour. `frontend/lib/weekly-recap.test.ts` covers
the URL state and view helpers. `backend/tests/test_decision_feedback.py` covers the feedback (as-of-time
enforcement, missing outcomes, no-archive weeks, grade math and bounds, own-team authorization on the legacy and /v2
routes); `frontend/lib/decision-feedback.test.ts` its view helpers.

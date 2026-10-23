# Availability timeline and roster digest (issue #176)

`GET /api/injury-impact/availability-timeline?team_id=` (also `/v2/leagues/{lg}/api/...` through the league scope),
page `/injury-impact/timeline` ("Availability" in My team). Code: `backend/app/analytics/availability_timeline.py`,
`frontend/lib/availability-timeline.ts`. Contract version `availability-timeline/v1`.

## Data sources (already integrated; nothing new is fetched)

| Source | Kind | What it gives | Time |
|---|---|---|---|
| NFL injury report via nflverse (`load_injuries`, table `nfl_injuries`) | official | game status (Out / Doubtful / Questionable), practice participation, injury, per player-week | provider `date_modified` when published; our retrieval time from `injury_retrievals` |
| NFL weekly roster via nflverse (`load_rosters_weekly`, `nfl_rosters_weekly`) | official | ACT, RES, INA, PUP, NFI, SUS, EXE ... per week | week only |
| League provider status (`fantasy_players.injury_status`, ESPN or Sleeper mapped to ESPN's words) | provider | current status | none from the provider; our sync time |
| Owner-curated news (`analytics/availability_news.json`) | third party | reviewed summaries of public reports, with their links | report date only |
| NFL schedule via nflverse (`nfl_schedules`) | schedule | kickoff | scheduled time |

Not available from any integrated feed, and never inferred: practice participation for each practice day. nflverse
publishes one practice value per report row (the week's final practice listed), so the page shows that value with
its scope, and `unknown` when the player is not listed. ESPN's historical roster snapshots are not used as a status
history (they may carry the current status; see the README).

## Rules

- **Unknown is explicit.** No current-week report row and no provider designation is `unknown`, with the reason
  (`not_listed`, `no_team_report`, `not_published`, `no_feed`, `unmapped`). Never healthy, never full practice.
- **Replacement.** Within a week the newest row (by UTC `date_modified`, then severity, exactly as
  `availability.injury_order_sql`) is `current`; older timed rows are `superseded`; untimed rows that disagree are
  `conflicting` and flagged. A newer week's report replaces the previous week's (`earlier_week`, `replaced_by`).
- **Separation.** Every event carries `source.type` (official, provider, third_party, schedule), its source name and
  link, and `at_basis` (published, retrieved, reported date, week only, scheduled). Third-party items only
  `supplement`.
- **No contradiction with lineup advice.** The headline level is `availability.availability()` over the provider
  status, the IR / taxi slot and the week's report (the input the roster, Start/Sit and Overview read). A
  disagreement between the NFL report and the provider is named, with the level the lineup tools use. The page links
  to Start/Sit and Injury impact instead of advising.
- **Changes this week.** A new, changed or dropped NFL report listing, a return to (or move off) the active roster,
  or a provider designation with no report yet.
- **Window.** The current week and the three before it. Times are UTC in the API and US Eastern on the page.
- **Roster scope.** Only the perspective team's current-week roster in the authorized league (`team_id` must be in
  the league: 404 otherwise). There is no player watchlist yet. Team defenses are left out.

## Cache note

The answer depends on the clock (`game.started`, freshness against kickoff, `generated_at`), like
`/api/injury-impact`. Under `/v2` it is cached by `league_scope`'s response cache, which keys on league, versions,
perspective, path and query but not time; the time bound for that cache is handled separately.

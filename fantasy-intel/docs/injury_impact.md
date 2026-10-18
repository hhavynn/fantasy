# Injury Impact Center (`/injury-impact`)

**Question.** Who is out or questionable this week, and whose role could grow if they sit?

Code: `backend/app/analytics/injury_impact.py` (every calculation), `backend/app/api/injury_impact.py`
(`GET /api/injury-impact`), `frontend/app/injury-impact/` (page), `frontend/lib/injury-impact.ts` (types, URL state,
grouping, labels). Tests: `backend/tests/test_injury_impact.py`, `frontend/lib/injury-impact.test.ts`.

Read-only. No new data source: it reads the tables the ESPN and NFL syncs already fill (`fantasy_players`,
`player_id_map`, `nfl_injuries`, `player_week_usage`, `nfl_weekly_stats`, `nfl_depth_charts`, `nfl_schedules`,
`espn_player_weekly_points`, `player_scores`, `injury_retrievals`).

## Who is listed

Every QB, RB, WR and TE whose status for the league's current scoring period is QUESTIONABLE, DOUBTFUL, OUT, IR or
SUSPENDED, taking the more severe of the ESPN status and the week's NFL report (`analytics/availability.py`). He must
have played for his current team this season and have a real role there: at least 25% of offensive snaps or 3 carries
plus targets per game (QB: 50% of snaps), or be on a fantasy roster in the league. Skipped players are counted
(`counts.skipped`: no game this season, small role, no team).

Absence kind, from the team's games after the last one he played: `new` (played the team's last game), `ongoing`
(missed 1-2), `long_term` (missed 3+; teammates' recent usage already reflects it). The page shows games still to play
first, long-term absences and games already kicked off / on a bye in collapsed groups.

## Two kinds of number

**Observed** (counted from this season's `player_week_usage`, current team only):

* the injured player's workload over the games he played: snap share (season and last 4), targets, carries, pass
  attempts (QB), target / backfield share, xFP and PPR per game, the role trend from `player_scores`, and a weekly
  snap-share series where a missed game is a gap, never a zero;
* each teammate's volume per game in team games the injured player played versus missed, with the games behind each
  side, and the change. "Missed" means team games while he was on the team (`nfl_rosters_weekly`, any status but
  released / retired / traded; without roster data, from his first game for the team on): games before he joined are
  not absences. A teammate's side averages over the games he was available for (on the roster and not on a reserve
  list, or he played), and a game he did not play counts as **zero**. A backup with no snaps while the starter played
  therefore has a real 0 baseline instead of a missing one. A week he was not on the team stays out of the sample. Volume is carries + targets for an RB absence (RB teammates) and targets for a WR or TE absence
  (WR, TE and RB teammates).

**Estimate** (labelled, dashed in the UI, never called a projection):

* *pro-rata share*: the injured player's volume over his last 4 games, split over healthy teammates in proportion to
  their volume in the last 3 team games **he played**. After he has already missed time, teammates' recent usage
  includes what they absorbed, so splitting by it would count that volume twice;
* *ESPN*: ESPN's own weekly projection, unmodified.

A QB absence has no split: it names the next healthy quarterback (depth chart, then usage), his pass attempts in games
without the starter, and the top three pass catchers' PPR per game with versus without him.

## Uncertainty about who absorbs the volume

There is no "winner" field. Each absence carries a clarity label and a sentence:

| label | meaning |
|---|---|
| `no_history` | no team game without him this season: only the estimate exists |
| `thin_history` | one team game without him: the observed change can easily be noise |
| `leaning` | 2+ games without him, and the teammate with the largest estimated share (at least 50%) also gained the most in observed games |
| `split` | otherwise: the volume spread out, or observed games and the estimate disagree |

The project's own research (`docs/research/qb_teammate_context.md`) found that proportional redistribution gets the
average uplift roughly right but which teammate realizes it is skewed and unpredictable. The `ifp-ctx` teammate-context
challenger is research only; this page does not import or show it (tested).

## Roster actions

For the selected team (`?team=`, default: the owner's), over games still to play and absences that are not long-term:
your injured players (link to Start/Sit), your healthy teammates of an injured player who could gain, and free-agent or
waiver teammates who could gain. A teammate "could gain" when the estimate gives him at least 25% of the vacated volume
and at least 2 per game, or he gained at least 3 per game in observed games without the injured player.

## Freshness

Per team, `analytics/injury_freshness.py` decides whether the injury evidence used before kickoff is fresh, stale
(retrieved more than 24 hours before kickoff with the game within 72 hours), missing, or retrieved only after kickoff.
Visible notices (`notices[]`): no NFL report synced, this week's report not synced yet, stale or missing teams on the
upcoming slate ("a missing report is not a clean bill of health"), ESPN statuses older than 24 hours, no schedule, no
usage history, no league rosters.

## API

`GET /api/injury-impact?position=ALL|QB|RB|WR|TE&status=all|out|questionable&team_id=`

`out` covers OUT, DOUBTFUL, IR and SUSPENDED. 422 for a bad filter, 404 for a team not in the league, 503 when the
league is not synced or its current week is unknown. The page reads the full list once and filters in the browser, so
the roster panel always covers every absence.

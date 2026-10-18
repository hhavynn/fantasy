# League Power Rankings

`GET /api/power-rankings` (contract `power-rankings/v1`) and the `/power-rankings` page rank every roster in the
league on a visible composite. Code: `backend/app/analytics/power_rankings.py`. Read-only: no writes, syncs or
ESPN calls.

Power rankings answer "which rosters are strongest right now". They are not player rankings (`/rankings`) and not
the standings or playoff scenarios (`/playoff-race`). Nothing here is a win probability.

## Components

Every component reuses an existing engine unchanged.

| Component | Weight | What it measures | Engine |
| --- | --- | --- | --- |
| Lineup strength | 45% | Average projected points of the optimal starting lineup per remaining week, on comparable weeks only (every team's projection complete) | `team_value.league_ranks` over `team_lineups.project_team` |
| All-play | 30% | All-play winning percentage over completed regular-season weeks | `playoff_race.all_play` |
| Depth and injury cover | 15% | Remove one starter at a time and re-optimize: projected points the rest of the roster recovers, averaged over the weeks he would start. Own roster only | `team_lineups.roster_marginal_value` |
| Replacement options | 10% | Per starting position (QB, RB, WR, TE), how far the best bench player projects above the best free agent each week, floored at 0, summed over positions. A position counts as covered at 0.1 per week or more | optimizer bench + `team_lineups.replacement_market` |

K and D/ST are left out of depth and replacement because every team can stream them; lineup strength values them
against the weekly free-agent stream (`trades.with_streaming`), as Team Value does. `kdst_streaming=false` turns that off.

## Composite

Each component is scaled within the league: the best ranked team scores 100, the worst 0, all equal 50. The power
score is the weighted mean of the four scores. A component no team has yet (no completed weeks) is dropped and the
others are scaled up. Equal power scores share a rank. Teams without a roster snapshot or without any complete
comparable week are listed unranked, never scored 0.

## Week-over-week movement

The rankings for week E describe the league before E's first kickoff, where E is the first fantasy week that has
not kicked off. Last week's ranks (P = E − 1) are recomputed point-in-time, never estimated:

* rosters from `lineup_snapshots` at scoring period P, only when they provably are the rosters at P's first
  kickoff: every row was synced before kickoff, or the rows carry acquisition dates and none is at or after kickoff.
  The sync rewrites a period's rows, so a snapshot synced later can include adds and trades made during the week;
  moves are not reversed from the transaction log because ESPN omits other teams' trade legs and some adds there;
* ESPN projections from the newest `espn_projection_history` snapshot whose rows were synced before P's first
  kickoff (`production_forecasts.first_kickoffs`);
* free agents = league players on no roster at P;
* all-play over periods that ended before P.

NFL availability records are current-only, so they are not applied to P (ESPN's projections already carry injuries).
If any input is missing, `previous.available` is false, `previous.reason` says why, and no movement is shown.

## Team card

`/power-rankings/<team id>` shows one team's card (rank, movement, power score, the four components with their
league scores and ranks, strongest and thinnest lineup slots, the starters hardest to replace and backups above
waivers), framed for a screenshot. The URL is the share link.

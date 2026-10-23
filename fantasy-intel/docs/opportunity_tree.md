# Opportunity tree (`/injury-impact`, "Depth charts and opportunity")

**Question.** When a starter is out or coming back, who does the data say gains or gives volume back, and can I add them?

Code: `backend/app/analytics/opportunity_tree.py`, `GET /api/injury-impact/opportunity-tree?team_id=` (served by the
injury-impact router, so it is mounted under `/v2/leagues/{lg}/api/...` with `league_scope` like the page it sits on),
`frontend/app/injury-impact/OpportunityTree.tsx`, `frontend/lib/opportunity-tree.ts`. Tests:
`backend/tests/test_opportunity_tree.py`, `frontend/lib/opportunity-tree.test.ts`, `frontend/e2e/opportunity-tree.spec.ts`.

Read-only. It reads the same context as the Injury Impact page (`injury_impact.load_context`: statuses, usage, weekly
rosters, depth charts, league rosters), plus the past schedule (coverage) and the external route participation already
stored in `player_week_usage`. No new source, no fetch. The research-only teammate-context challengers are not used.

## Scenarios

| kind | who |
|---|---|
| `teammate_unavailable` | every player the Injury Impact page lists, except a questionable player who missed the team's last game |
| `teammate_returning` | a player with a real role (same thresholds as Injury Impact) who missed the team's last game while on the team and is not designated out, doubtful, IR or suspended |

`certainty` is how settled the change is, never "confirmed": `designated_out` (Out / IR / suspended), `if_he_sits`
(questionable / doubtful), `if_he_plays` (questionable after missing the last game), `status_cleared` (on this week's
NFL report without an Out / Doubtful designation, or a provider status of active), `unconfirmed` (no such evidence).
A missing designation is never read as a return. Depth charts, coach statements and backup order are never evidence.

Each scenario carries its sample (`team_games_with`, `team_games_without`, `weeks_without`) and, per named teammate, the
observed volume per game with and without the starter (Injury Impact's split: games he was available for; a game he
did not play counts as zero; weeks off the team are left out).

## Who is named

* **observed**: at least 2 team games on each side and an observed change of at least 3 touches or targets per game
  (10 pass attempts for a QB). For a return, the teammates who gained that much while he was out.
* **hypothesis**: only when there are fewer than 2 games without the starter, and the team has a depth chart: the next
  healthy player at his position on the depth chart, and any teammate whose pro-rata share is at least 25% and 2 per
  game. Labelled "Hypothesis", dashed, never shown as observed.
* `no_supported_beneficiary`: 2+ games without him and nobody gained enough: the volume spread out.

## Abstention

A scenario names nobody (`outcome: "abstain"`, with a reason) when:

* `coverage_gap`: the schedule says the team played a past week that has no usage rows;
* `coverage_unverified`: the past schedule is not synced, so gaps cannot be checked;
* `depth_unknown`: the team has no depth chart and there are fewer than 2 games without the starter;
* `no_return_evidence`: a return with no pregame evidence.

## Depth chart

One group per scenario position: players at that position on the team (latest usage team or latest depth chart, not
released / traded on the newest weekly roster), in depth-chart order, or by recent snap share when the team has no depth
chart (said in words). Per player: depth rank, availability (`UNKNOWN` when neither the provider nor the NFL report
covers him), and over the games he played among the team's last 3: snap share, route share (only where an external
provider supplied values; the provider is named only then) and touch share (carries + targets over the team's
skill-player carries + targets in those games). Recent personnel changes are facts with weeks: first game for the
team, missed the last N games, back after missing N, reserve list on the newest weekly roster.

## Ownership

From the league's own roster data (`fantasy_players` in the league's files), for the selected team (`?team_id=`):
`yours`, `opponent` / `rostered` (never claimable), `waivers` (needs a claim), `free_agent` (no claim needed, only where
the provider reports waivers), `available_claim_unknown` (Sleeper: every unrostered player is synced as a free agent, so
whether a claim is needed is unknown), `unknown`. The page links your own players to Start/Sit and claimable players to
the add/drop check on Waivers (`/waivers?pos=..&add=<id>#add-drop`), which applies the league's claim rules.

# Stash planner (issue #218)

`GET /api/my-team/stash?team_id=&horizon=2|4|6&kdst_streaming=` (`analytics/stash_planner.py`) answers a multi-week
question for one roster: which free agents are worth a bench spot now for what they add to this roster's starting
lineup in later weeks. It is read-only, served per league under `/v2/leagues/{id}` like the rest of the team value
router, and kept apart from current-week waiver advice.

## The exact part

Over the current week plus the next `horizon` weeks (default 4), every candidate is the #216 evaluator's exact two-lineup
difference (`marginal_utility.scenario`): this roster with the free agent added and the drop candidate removed, minus
this roster as it is, week by week.

Only a candidate whose net difference over the whole horizon is positive is listed.

| Group | Rule | Shown as |
|---|---|---|
| Pick up now | adds this week, and nets a gain over the horizon | a current-week decision for the Waivers page, never listed as a stash |
| Stash | adds nothing this week (or costs points this week, which the why-now line states) and more than makes it up later | the stash list, with the weeks he starts and whom he fills in for |
| No gain | nets nothing over the horizon | counted only |

- **Drop candidate.** The active player whose removal costs this roster least over the same weeks (exact; ties by player
  id), or nobody when an active roster spot is open and the active count is exact. When `trades.roster_legality` says
  the count is unverified (players missing from the synced data, or no count for the team), an open spot is never
  assumed: the least-cost drop is used, `drop_basis` is `roster_count_unverified` and `drop_notes` gives the reason.
  A league with an undroppable list adds a note that the drop's status on it is not known. IR and taxi players are never
  dropped or moved. One drop is used for every addition, so a position at its roster limit stays closed unless that drop
  plays it.
- **Candidates.** Free agents whose availability is known (`FREEAGENT` or `WAIVERS`), at QB, RB, WR or TE (K and D/ST are
  streamed week by week, #181), projected in every horizon week, and legal under the league's position limits after the
  drop. Every cut is counted (`counts`).
- **Gain bound.** Before any lineup is solved, each candidate gets an upper bound: per week, his projection (0 in a week
  he is verified NFL-ineligible) minus the lowest starter he could push out through the slots his position reaches in
  the lineup without the drop (0 for an empty slot), plus the drop's exact removal cost. Adding one player changes an
  optimal lineup by one replacement chain that ends with that starter leaving, so a bound of 0 or less is no gain
  (`no_possible_gain`, exact, not solved). A week with a missing or negative projection on the roster (or a negative one
  for him) has no bound, so he is solved: a negative player can be forced into a fixed slot and the flex solver may
  leave flex slots empty. When removing the drop alone adds points, every free agent is measured against the roster
  without him and a `drop_notes` entry reports the drop's own change. A randomized test checks the bound against the evaluator. The rest are
  evaluated exactly, at most 50 per position in bound order (`not_evaluated_cap`), so free agents who can never start
  (a backup QB in a one-QB league) never crowd out a real stash.
- **Unknown is not 0.** A missing projection in a horizon week excludes the candidate (`excluded_unprojected`). If every
  active player's removal cost is unknown, no drop can be chosen and the plan says so (`drop_unknown`); if every
  evaluated value is unknown, the reason says that. A projected 0 (a bye) is a real value.
- **Injuries.** Only projections and verified NFL unavailability enter the lineups; a designation such as questionable
  changes nothing by itself, and no injury probability is used.
- **Ranking policy.** Net horizon gain, then the earliest later week he starts, then player id.
- **Why now.** For each stash, this week's cost if any, the weeks he starts and whom he replaces in the lineup (the drop
  is marked), and his projection this week against the later average.

## The contingency part

From the injury opportunity tree (#274, `docs/opportunity_tree.md`): when a starter is out or returning, the teammates
whose observed volume changed (or, labelled `hypothesis`, the next on the depth chart) and who are claimable in this
league (free agent, on waivers, or available with claim status unknown). Each item is an if-then statement with its
sample and observed volume, the team's game this week (kickoff, started, bye), the injury report's freshness, the depth
chart's as-of and the usage coverage, plus the tree's `generated_at`, `sources` and notices. It carries no lineup number
and no probability that the event happens, and is this week's evidence: it expires with the team's game. Abstaining
scenarios are counted by reason (`abstained_by_code`, for example `depth_unknown`); when the tree cannot be built (the
league or NFL data not synced) the section says so.

## What it does not do

- No injury, role-change or acceptance probability anywhere.
- No waiver guarantee: a player on waivers needs a claim, and FAAB bids are the claim planner's job.
- No backtest yet: the plan is a data-qualified v1 overview. An as-of backtest of stash signals on held-out weekly
  outcomes is the next step (#218 plan item 5).
- No page yet: the UI is a later PR, coordinated with the Waivers page owners.

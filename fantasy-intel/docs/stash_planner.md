# Stash planner (issue #218)

`GET /api/my-team/stash?team_id=&horizon=2|4|6&kdst_streaming=` (`analytics/stash_planner.py`) answers a multi-week
question for one roster: which free agents are worth a bench spot now for what they add to this roster's starting
lineup in later weeks. It is read-only, served per league under `/v2/leagues/{id}` like the rest of the team value
router, and kept apart from current-week waiver advice.

## The exact part

Over the current week plus the next `horizon` weeks (default 4), every candidate is the #216 evaluator's exact two-lineup
difference (`marginal_utility.scenario`): this roster with the free agent added and the drop candidate removed, minus
this roster as it is, week by week.

| Group | Rule | Shown as |
|---|---|---|
| Stash | adds nothing this week, adds starting points in later weeks | the stash list, with the weeks he starts and whom he fills in for |
| Pick up now | already adds this week | a current-week decision for the Waivers page, never listed as a stash |
| No gain | adds nothing in the horizon | counted only |

- **Drop candidate.** The active player whose removal costs this roster least over the same weeks (exact; ties by player
  id), or nobody when an active roster spot is open. IR and taxi players are never dropped or moved.
- **Candidates.** Free agents whose availability is known (`FREEAGENT` or `WAIVERS`), at QB, RB, WR or TE (K and D/ST are
  streamed week by week, #181), projected in every horizon week, and legal under the league's position limits after the
  drop. Per position, a free agent projected no higher than another in every horizon week is dominated and not
  evaluated; at most 40 are evaluated, by later-week projection. Every cut is counted (`counts`).
- **Unknown is not 0.** A missing projection in a horizon week excludes the candidate (`excluded_unprojected`). If every
  active player's removal cost is unknown, no drop can be chosen and the plan says so (`drop_unknown`). A projected 0
  (a bye) is a real value.
- **Ranking policy.** Later-week gain, then the earliest week he starts, then player id.
- **Why now.** For each stash, the weeks he starts and whom he replaces in the lineup (the drop is marked), and his
  projection this week against the later average.

## The contingency part

From the injury opportunity tree (#274, `docs/opportunity_tree.md`): when a starter is out or returning, the teammates
whose observed volume changed (or, labelled `hypothesis`, the next on the depth chart) and who are claimable in this
league (free agent, on waivers, or available with claim status unknown). Each item is an if-then statement with its
sample and observed volume. It carries no lineup number and no probability that the event happens. Abstaining scenarios
are counted; when the tree cannot be built (the league or NFL data not synced) the section says so.

## What it does not do

- No injury, role-change or acceptance probability anywhere.
- No waiver guarantee: a player on waivers needs a claim, and FAAB bids are the claim planner's job.
- No backtest yet: the plan is a data-qualified v1 overview. An as-of backtest of stash signals on held-out weekly
  outcomes is the next step (#218 plan item 5).
- No page yet: the UI is a later PR, coordinated with the Waivers page owners.

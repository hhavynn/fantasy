# V2 My Team Value dashboard (read-only)

Code: `backend/app/analytics/team_value.py` (service), `backend/app/api/team_value.py` (`GET /api/my-team/value`),
the `/my-team/value` page (nav "Team value"). Tests: `tests/test_team_value.py` (controlled synthetic leagues in
`tests/synthetic_race.py` and `tests/synthetic_value.py`, plus the ESPN fixture for the database and API tests).

The dashboard answers six questions for one team (your `is_mine` team by default):

1. How strong is the roster against the league?
2. Who actually improves the starting lineup?
3. Where does projection sit unused on the bench?
4. Where would an upgrade help most?
5. Which trades or waiver moves help?
6. How does all of this differ between the remaining regular season and the fantasy playoffs?

It writes nothing and changes no other module.

## What already existed, and what this PR calculates

| Need | Reused unchanged | Calculated here |
|---|---|---|
| Remaining starting-lineup points (all / regular / playoff), average weekly, coverage | `team_lineups.project_team` summary; `trade_data.load_league_state` (ESPN weekly projections, current rosters, kickoff-aware first week) | Phase splits with incomplete weeks listed; league median per week (only weeks where every team is complete) |
| League-relative rank | `project_team` for every team | Ranks on **comparable weeks only** (see below) |
| Player marginal value, regular/playoff marginal, weeks starting, weekly breakdown | `team_lineups.roster_marginal_value` (as `/api/teams/{id}/lineup-projection` uses it) | Bench weeks, each player's own ESPN projected total per phase |
| IR context and activation upside | `roster_marginal_value` → `ir_activation_sensitivity` | — (shown separately, never in a total) |
| Positional strength | `project_team` assignments | Average projected points each team gets from each starting slot group per comparable week, with the team's rank |
| Depth | `team_lineups.without` + `project_team` (explicit removal) | Who fills in for each starter, which weeks, and how much of his points the roster recovers |
| Bench reasons | lineup assignments; `lineup.eligible`; `player_ros_projections.bye_weeks` | A reason code per benched week: IR, no projection, projected 0, NFL bye, FLEX competition, outscored |
| Upgrade slot-weeks | `team_lineups.replacement_market` | Gap between your weakest starter in a slot and the best available free agent for that slot-week |
| Waiver moves | `team_lineups.best_fa_addition`; stored `waiver_recommendations` | Realistic add/drop (best free agent per candidate drop, net exact lineup delta); roster-capacity label; ESPN position limits enforced with the trade engine's rule (a free agent that would exceed one is skipped and counted); stored waiver suggestions re-checked on the lineup engine |
| Trades | `trade_discovery.TradeSearch` + `detail` (manual evaluator consistency), `trades.evaluate_trade` | A small 1-for-1 preview (both-gain list and my-gain list), with links to the evaluator |
| Playoff race | `playoff_race.build_playoff_race` (PR #13) | Re-projection of your remaining matchups after the best add/drop and the top trade preview |

## Rules

**Comparable weeks.** A week counts toward league ranks and the league median only when **every** ranked team's
optimal-lineup projection is complete that week. If any team is missing a projection, the week is left out for
everyone, and it is listed with the incomplete teams. Your own full forecast is still shown separately, so no complete
forecast is compared with a partial one. A team without a roster snapshot is unranked; it is never counted as 0.

**Marginal value** is your optimal starting-lineup total with the player minus without him, week by week. Bench
weeks add 0, and a current-IR player adds 0. It depends on the rest of the roster, so:

* it is never summed into a team number;
* it is not trade currency.

**Missing vs zero.** A missing projection makes the week incomplete. The canonical total is NULL, and the known-only
total is shown and labelled "known … (incomplete)", always for the selected phase only (a missing playoff week never pulls
regular-season points into the playoff view). Add/drop rows with incomplete deltas are ordered by those partial totals and
say so. A projected 0 (a bye, or out) is a real value.

**IR.** Current-IR players are inactive, as in the canonical engine. Their hypothetical free-activation upside
(`ir_activation_sensitivity`) is shown next to them and never enters a total.

**Depth.** Depth comes from removal scenarios: the roster without one starter, re-optimized every week. This
describes who covers him today. It is not an injury prediction, and no depth multiplier is used.

**Bench value.** Unused projected points are a player's ESPN projections in the weeks he sits. They are listed per
player and never summed, because bench alternatives are mutually exclusive. Players with the most unused projection
are marked consolidation candidates. That is **not** a sell call, since he may be worth more to another roster.

**Waivers.**

* The best free addition is labelled realistic only when an active roster spot is open. Otherwise it is marked
  optimistic: it needs a drop.
* Add/drop takes the three active players whose removal costs the least. For each one it finds the best free agent
  to add (`best_fa_addition` on the roster without him) and reports the net exact lineup delta against today's roster.
  On equal gains, it drops the lower-marginal player.
* The waiver page's stored suggestions (CLAIM/WAIT with a suggested drop) are re-checked on the same engine. Their
  waiver score is the waiver page's heuristic, not a lineup projection.

**Trades.** Only 1-for-1 trades are previewed, for speed. Each row is the manual evaluator's exact delta for both
sides. The full search is on Trade discovery. There is no acceptance estimate.

**Playoff race.**

* Your record, the scenario standing, the cutline and your remaining matchups (with close games) come straight from
  the Playoff Race Lab.
* For the best add/drop and the top positive trade preview, every remaining upcoming matchup is re-projected with
  the same rules: both sides' optimized weekly totals over the matchup's weeks. For a trade, the partner's lineup is
  re-projected too.
* `flips` means the projected winner changes sides. `changes_projected_result` also covers a change to or from an
  even matchup.
* This is a deterministic scenario, not a win probability.

## K / D/ST streaming

By default every lineup on this page, for every team, can stream K and D/ST at the weekly baseline (the 3rd-best
available player's projection). This covers the overview, marginal values, upgrades, the trade preview and matchup
effects; the matchup-effect "before" margins are re-projected under the same model. `?kdst_streaming=false`
restores the hold-all-season view. See [`kdst_streaming_baseline.md`](kdst_streaming_baseline.md).

## API and page

`GET /api/my-team/value?team_id=&trades=true|false` returns:

* team and league;
* the window and freshness;
* the standing;
* `overview` (summary, phases, weekly with league median, league ranks);
* `players` (marginals, weekly starts, bench reasons, IR sensitivity);
* `positions` (slot strength by phase, removal scenarios, bench coverage);
* `bench`;
* `upgrades` (slot gaps, free addition, add/drop, waiver checks, trade preview);
* `playoff_race`.

It is read-only. An unknown team returns 404, and no synced league returns 503.

The page has:

* a team selector and a rest-of-season / regular-season / playoffs toggle;
* an executive summary that answers the questions in one place;
* a weekly projected-starting-points chart with the league median;
* starting slots vs the league best, with ranks and the biggest single-player loss;
* the marginal-value ranking with a weekly start strip;
* the bench table with reasons and the starters each bench player covers;
* upgrades;
* the playoff-race context with before/after matchup margins;
* links to player profiles, Trade discovery, the trade evaluator (prefilled), Waivers and the Playoff Race Lab.

## Performance

Measured on synthetic leagues:

* about 20 ms for 6 small rosters;
* about 1.1 s for 12 teams × 17 players with 250 free agents, including the trade preview (Trade discovery stage A
  plus 3–6 exact evaluations) and four `best_fa_addition` searches;
* `trades=false` skips the trade preview;
* the payload is about 95 KB.

## Validation and limitations

Synthetic only. No local ESPN database exists in the development environment, so **nothing was validated against
real Los Subies data**, and the synthetic tests say nothing about real outcomes.

* Rosters are ESPN's current snapshot for every remaining week.
* Projections are ESPN's, unmodified. Bye detection uses `player_ros_projections.bye_weeks` when present; otherwise a
  projected 0 is labelled "projected 0".
* Add/drop examines three drop candidates (the cheapest removals) and one free agent per drop. It is not an
  exhaustive multi-move search. Waiver claims are not guaranteed.
* The trade preview is 1-for-1 only; 2-for-1 and 1-for-2 trades are on Trade discovery.
* Slot-week gaps are single-slot, single-week ceilings, not lineup totals.
* League ranks need comparable weeks. If every week has some team incomplete, no rank is shown.

## Roster utility (issue #216)

`GET /api/my-team/value/marginal?ids=…&pool=bench|waiver` answers one question about this roster: what removing one to
three of its players changes in its exact optimal starting lineups, week by week, and who fills in. It is the evaluator
in `analytics/marginal_utility.py` (#216 PR 1) on the same league state, streaming setting and window as this page.

* **Jointly, never summed.** Several players are removed together and evaluated as one lineup. The answer also lists
  each one's single removal and says whether the joint loss differs from their sum (one can be the other's backfill).
* **Explicit replacement.** `bench` (default): the rest of the roster moves up. `waiver` (one player at a time): plus
  the best fully projected free agent whose availability is known (`FREEAGENT` or `WAIVERS`: ESPN's synced status;
  Sleeper infers `FREEAGENT` from not being rostered) and whose addition keeps every position within the league's roster
  limit once the removed players are gone. The league state already loads only such free agents, so the availability
  filter is defensive; anyone excluded is counted (`waiver_availability`), and with no free agent loaded the pool is
  `unknown_pool` and no replacement is assumed. A hypothetical acquisition is the trade analyzer's question, not this
  page's.
* **Unknown is not 0.** A week with a missing projection has no delta, and neither does any total that contains it (the
  known part is shown marked). Players in IR or taxi slots start no lineup, and the answer says so.
* **K and D/ST streaming.** With streaming on (the page's default), a removed K or D/ST can be replaced by that week's
  waiver streamer even in the bench pool; the answer then says so and shows the streaming note.
* **Labelled.** Every answer names its unit, the projections it used, the baseline it changes, the evaluated weeks, and
  that lineup locks and weeks after the final fantasy week are not modelled. When the page narrows a question (more
  than three players chosen, or a free-agent replacement asked for several players) it says so.

On the page, the "Roster utility" section is a plain GET form (`mu` repeated for the players, `pool`); the answer
streams in under it with the replacement path week by week. It is read-only, and the route is served per league under
`/v2/leagues/{id}` like the rest of this router.

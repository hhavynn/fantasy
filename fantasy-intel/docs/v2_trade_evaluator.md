# V2 trade evaluator: exact weekly lineup simulation

Code: `backend/app/analytics/team_lineups.py` (pure lineup / marginal-value / replacement-market functions),
`backend/app/analytics/trades.py` (pure trade engine), `backend/app/analytics/trade_data.py` (database side),
`POST /api/trades/evaluate`, the `/trades` page. Tests: `tests/test_team_lineups.py`, `tests/test_trades.py`,
`tests/test_trade_data.py`.

## Core idea

A fantasy trade is worth what it does to the two teams' actual starting lineups over the remaining weeks.
For each team, independently:

```
roster before  ->  optimal lineup every remaining week  ->  projected starting points
apply trade    ->  resolve roster capacity (forced drop if needed)
               ->  optimal lineup every remaining week again  ->  projected starting points
delta = after - before
```

That exact lineup delta is the canonical trade impact. There is no per-player trade value that gets added
up (`A = 50, B = 25, C = 25, so B + C = A` is never computed).

## ESPN is the forward points baseline

A player's value in week *w* is `espn_player_weekly_points.projected_points` for that week, exactly as ESPN
published it. Nothing multiplies it: no xFP, role, matchup, schedule, injury-probability or BUY/SELL factor.
Our analytics (opportunity tier, role trend, production sustainability, market signal, player-context
labels, injury status, manager profiles) are shown next to the result as explanation only.

## Weekly exact lineup optimisation

Every remaining fantasy week is optimised separately with the league's real `roster_slots` and the existing
exact FLEX assignment in `analytics/lineup.py` (fixed slots greedily, then an exact search over FLEX,
RB/WR, WR/TE and OP; ties break by player id; a FLEX is one slot). Because each week is solved on its own,
byes, projected injury zeros, positional depth and changing weekly projections are handled naturally: a
player can start week 8, sit week 9 and start again week 10. Only starters count; bench points are never
value.

Each team-week records the slot-by-slot assignment, the bench, the known total, the canonical total, the
completeness flag and the players without a projection (`team_week_lineup_projections`); the season summary
is in `team_ros_lineup_projections`. Both tables are derived and rebuilt by analytics.

## Missing is not zero

* A projected **0** (bye, injury) is a real value and can start (it produces 0).
* **No projection** is unknown. The player never enters the lineup as a 0. `assign_lineup()` treats a
  missing value as 0 in other contexts, so the V2 wrapper only passes it players that have a projection.
* If a lineup-relevant rostered player who has a current NFL team lacks a projection, the team-week is
  incomplete: `projected_lineup_points` is NULL, the player is listed in `missing_projection_ids`, and
  `known_lineup_points` (the optimal lineup from known projections only) is still reported, labelled as such.
* A teamless player (no NFL team) can legitimately have no projection; he is listed in
  `teamless_unprojected_ids` and does not make the week incomplete. A player at a position with no starting
  slot in the league is irrelevant to the lineup.
* Every canonical aggregate (team ROS / regular season / playoffs, trade deltas, marginal values, forced-drop
  cost) is NULL unless every week in it is complete. The API also returns the known-only figure.

## Evaluation start week

ESPN's `current_week` can point at a scoring period whose games are already over. A trade made after Monday
night of week 6 must not get credit for changing week 6. `trade_data.evaluation_start_week()`:

```
if every REG game of ESPN current_week has a final score (home_score and away_score) in nfl_schedules:
    evaluation_start_week = current_week + 1
else:
    evaluation_start_week = current_week
clamped to final_scoring_period
```

Completion comes only from game scores, never from player stats existing, so a week with Thursday played and
Sunday pending is not skipped. If completion cannot be established (no schedule table, no score columns, no
REG games for the week) it defaults to `current_week` and says why. The reason is returned with every
evaluation. The pure evaluator and the API accept an explicit `start_week` override (from ESPN `current_week`
through the final period).

## Regular season and playoffs stay separate

The split comes from league settings (the last scoring period of matchup `reg_season_matchups`; see
`docs/espn_ros_projections.md`). Regular-season, playoff and total deltas are reported independently. There is
no playoff weighting (no "playoffs = 25%" or "2x"); the user or a future recommender decides how much
playoffs matter.

## Roster marginal value

`roster_marginal_value()` answers "how many projected starting points does this player create for THIS
team?": optimal lineup with him minus optimal lineup without him, week by week, aggregated for the regular
season, playoffs and the full remaining season (`roster_marginal_lineup_points`). Depth, FLEX, byes,
redundant bench production and concentration are all automatic; a player who never starts is worth 0 to
that roster no matter what ESPN projects him for. Shown for every traded player (on his current team) and per
roster in `GET /api/teams/{id}/lineup-projection`.

## Replacement market (available-player frontier)

For each remaining week, the players whose ESPN `availability_status` is `FREEAGENT` or `WAIVERS` and who have
an ESPN projection that week, grouped by every fixed position the league starts (QB, RB, WR, TE, K, D/ST) and
by each flex slot type the league actually has, with that slot's real eligibility (FLEX = RB/WR/TE). For each
group/week: available count, projected count, unprojected count, best available player and projection, and the
top five. Rostered players are excluded; unprojected free agents count toward availability but are never given
a manufactured projection. This is not a "trade value"; it is the league's real waiver frontier
(`GET /api/lineup-projections`).

## Hypothetical FA replacement (separate from the trade delta)

For each outgoing player: if he disappeared and his team could add one currently available player, which one
maximises the team's remaining projected starting-lineup total? Candidates are free agents who can fill at
least one slot the outgoing player could fill (an RB can be replaced by an RB, or by a WR/TE through FLEX; a QB
by a QB) and whom ESPN projects in every remaining week. Without that restriction an unrelated empty slot
elsewhere (e.g. no D/ST rostered) would always win, which is a roster-hole fill, not a replacement. Reported:
replacement player and position, lineup points recovered, residual loss vs the original roster.

The search is exact: per position, free agents weakly dominated week-by-week by another free agent at the same
position are skipped (same position = same slot eligibility, and the optimal total is non-decreasing in one
player's weekly value), and every remaining candidate is simulated.

Labelled **hypothetical FA replacement** because waiver availability is not guaranteed. It is never part of
the canonical trade delta.

## Trade evaluation

`evaluate_trade(state, team_a, team_b, a_sends, b_sends, start_week=None)` supports 1-for-1, 2-for-1, 1-for-2
and 2-for-2. It validates that the teams differ, every player is on his sending team's current roster, no
player appears twice, and each side sends 1 or 2. It never mutates the league state, `lineup_snapshots`,
rosters or anything at ESPN; the API endpoint is read-only.

For both teams it returns before/after summaries, the week-by-week delta (with who entered and left the
lineup), ROS / regular-season / playoff deltas, average weekly delta, weeks improved / worsened / unchanged,
slot-by-slot lineups for every week, how often each incoming player starts and in which slot (RB2, FLEX...),
who loses or gains starts, forced drops, open roster spots and incomplete-data details.

## No consolidation tax

There is no elite-player bonus, 2-for-1 tax or star premium. The exact lineup simulation already captures
consolidation: giving an elite WR for a WR2 + RB2, if only the WR2 starts, the RB2 contributes 0 starting
points in those weeks; if both start, both get credit; if the RB2 only starts during a bye, he gets credit for
that week only.

## No generic trade-chart addition

Player values are not summed. The only quantity is the change in each team's optimal starting lineup, which
depends on that team's roster, the league's slots and every week's projections.

## Roster capacity and forced drops

Active capacity = starting slots + bench slots from the league's `roster_slots` (IR excluded). Incoming players
always take active spots; they are never assumed to go to IR. That includes a player traded out of an IR slot: the
receiving side treats him as an active-roster asset (no assumed IR eligibility or open IR slot), so he counts
against capacity and can trigger a forced drop. ESPN `rosterSettings.positionLimits` (per
position maximums) are also enforced when present. Limits a team already exceeded before the trade are not
blamed on the trade.

If the post-trade roster is illegal, every legal removal set of the minimum size is enumerated and the one that
keeps the highest remaining projected starting-lineup total is chosen: the forced drop is the least costly
player in actual future lineup terms, not the lowest value score or raw projection. Ties break by player id.
Reported: `forced_drop_id`, `forced_drop_name`, `forced_drop_lineup_cost` (NULL with a known-only figure when
projections are incomplete).

A newly acquired player is never the default drop. If cutting one would be strictly cheaper than the best
legal drop of an existing player, the result carries `redundant_incoming_asset` (who, and how many lineup
points cutting him would save) instead of hiding the problem.

**ESPN undroppable-list restrictions are not modeled** (the league settings say whether the league uses ESPN's
undroppable list, but the list itself is not in our data). A forced drop may name a player ESPN would not let
you drop.

## Open roster spots

A team that sends 2 and receives 1 gets `open_active_roster_spots` (+1). Nothing is added to the canonical
after-trade roster. `hypothetical_fa_fill` shows the best available addition (any position, so it can reveal
a real hole such as no rostered D/ST), separately and never in the delta.

## Byes and injuries

No separate bye or injury penalty exists. ESPN's weekly projection already expresses expected output: a bye is
a projected 0 that week; an injured player's projection is ESPN's. Injury status, player context, production
sustainability and role trend are shown as warnings/context, never multiplied in (no "QUESTIONABLE = 70%",
no "IR return probability = 40%").

## Current IR players

The current roster is held constant going forward, so a player in an ESPN IR slot (`on_ir`) stays rostered but
**inactive** until a future sync shows ESPN moved him off IR:

* he never enters a canonical optimal lineup, even when ESPN projects him for future weeks;
* he uses no active roster spot;
* his projection, present or missing, never makes a team-week incomplete;
* his canonical `roster_marginal_lineup_points` is 0, and there is nothing to replace in `replacement_context`.

His ESPN projection is not deleted or zeroed: each week lists it under `ir`. What he could add is reported
separately as `ir_activation_sensitivity`: the same roster with him activated for free, minus the canonical
roster (projected weeks, weeks he would start, lineup-point upside by ROS / regular season / playoffs, and
whether the active roster has an open spot). With no open spot the result says "Activation would require a
roster move/drop that is not modeled in this sensitivity scenario." No activation drop is chosen, so the
sensitivity is deliberately optimistic, and it is never part of a trade delta. A trade shows it per side, before
and (if he stays) after the trade; `/trades` warns whenever a current IR player is involved.

## Result semantics

Labels come from the exact ROS deltas, with zero as the only dividing line (no "+2 points" threshold):
`MUTUAL_PROJECTED_GAIN`, `BOTH_LOSE`, `NO_PROJECTED_CHANGE`, `TEAM_A_GAIN_TEAM_B_LOSS`,
`TEAM_A_LOSS_TEAM_B_GAIN`, `TEAM_A_*_TEAM_B_UNCHANGED` / `TEAM_A_UNCHANGED_TEAM_B_*`, and `INCOMPLETE_DATA`
when either side lacks a canonical total. Regular-season and playoff labels are reported too.
`combined_ros_delta` and `min_side_ros_delta` (the smaller of the two deltas) are descriptive ranking fields
for a future trade finder, not a score. There is no "chance they accept" and no fairness grade.

## HOLD / BUY / SELL are context

Every traded player shows ESPN ROS and playoff projections, his marginal lineup value on his current team,
opportunity tier, role trend, production sustainability, market signal, player-context labels and injury
status. HOLD is not untouchable, SELL is not must-trade, BUY is not accept-any-price. The trade math is lineup
impact. Manager context (team position needs, manager classifications) is secondary; no acceptance
probability is computed.

## Known limitations

* ESPN projections can be wrong.
* Projections are not adjusted by our context yet.
* Future injuries are unknowable.
* Future waiver acquisitions are not guaranteed.
* ESPN undroppable-list restrictions may not be modeled (they are not).
* Manager acceptance is not predicted yet.
* Each team's current roster is assumed to be its roster for every remaining week.
* Current IR players stay inactive until ESPN moves them off IR; their return is only an optimistic, separate
  sensitivity (the activation roster move is not modelled).
* Nothing searches for trades automatically yet; this is the auditable primitive a future finder will use.

# Stash planner as-of backtest (#218 plan item 5)

Research only. Nothing here changes the product. The stash planner (`app/analytics/stash_planner.py`, `docs/stash_planner.md`)
ships as a data-qualified v1 overview. This backtest asks whether its decision rule earns that place on held-out
outcomes.

## Pre-registration (fixed before the first run)

### Question

Given the same as-of projections, does the planner's top stash realize more later-week starting points than a fair
lineup-unaware pick of the same kind, and more than doing nothing?

The test holds projection skill fixed across all arms. It tests the planner's decision rule: exact lineup difference,
drop candidate, net horizon gain, current-week adds kept apart. It does not test any projection model.

### Data (free, public)

- nflverse `player_stats` (regular season) for 2022-2025: PPR fantasy points per player-week.
- nflverse `schedules` for the same seasons: a team with no game in a week is on bye.
- Seasons evaluated: 2023, 2024, 2025. 2022 supplies the first prior season.
- Positions: QB, RB, WR, TE.

### As-of projection

At decision week `w`, a player's projection for week `k >= w` is:

- 0 if his team is on bye in week `k`. His team is the one he played for in his last game before `w`; byes are
  scheduled in advance.
- Otherwise a shrunk rate: `(points in this season's games before w + 3 * prior_ppg) / (games before w + 3)`, where
  `prior_ppg` is his previous-season PPR points per game.
- A player with no previous-season games uses his raw rate this season, if he has at least 2 games before `w`.
  Otherwise he is unprojected and excluded (unknown is not 0, as in the planner).

The same projections feed every arm.

### Leagues

- Per season: 10 synthetic 12-team leagues.
- Each is drafted by snake draft over 15 rounds, from preseason value = prior-season PPR total times a lognormal noise
  factor with sigma 0.25 (seeded per league).
- Position caps: QB 2, RB 6, WR 6, TE 2.
- Lineup: QB, RB, RB, WR, WR, TE, FLEX (RB/WR/TE), plus 8 bench.
- Rosters are static (no in-season transactions). Each decision is an independent, counterfactual single add/drop on
  that week's roster.
- The free-agent pool at week `w` is every undrafted QB/RB/WR/TE who is projected, all treated as free agents.
- Decision weeks: `w` = 3..13. The horizon is 4 later weeks (`w+1..w+4`, all within week 17).

### Arms (per league, team and week)

- **PLAN**: `plan_stashes(horizon=4)` with the as-of projections. Its top stash and its drop candidate. Team-weeks
  with no stash are counted, not compared.
- **B1, projected-total greedy**: the free agent with the highest projected total over `w+1..w+4`, at any position
  (lineup-unaware), with the same drop. Ties are broken by id.
- **B2, this-week waiver default**: the free agent with the highest projection for week `w`, with the same drop.
- **NONE**: no move.

### Outcome

The realized gain of a pick `f` with drop `d` is the sum, over `w+1..w+4`, of the exact optimal lineup of the roster
minus `d` plus `f`, minus the exact optimal lineup of the roster. It uses actual points, with the same lineup engine as
the planner and the same slots. Actual points are 0 when a player did not play. Every arm is scored the same way.

### Decision rule

On team-weeks where PLAN returns a stash:

- `D1 = R_PLAN - R_B1`
- `D2 = R_PLAN - R_B2`
- `R_PLAN` itself is compared against NONE, which scores 0.

For each, report the mean and a 95% percentile bootstrap CI (2,000 resamples, seed 218), under two clusterings: by
(season, week) and by (season, league, team).

| Outcome | Condition |
|---|---|
| **Supported** | `mean(R_PLAN)` > 0 and `mean(D1)` > 0, each with its CI entirely above 0 under both clusterings |
| **Baseline better** | the `D1` CI entirely below 0 under both clusterings |
| **No evidence** | anything else |

`D2`, precision, calibration and the no-stash team-weeks are reported but do not decide the outcome.

- **Precision**: the share of PLAN stashes with `R_PLAN > 0`.
- **Calibration**: the projected later gain against `R_PLAN`.
- **No-stash team-weeks**: `R_B1` where PLAN found no stash.

### What this cannot show

- Real leagues trade and claim players. Static synthetic rosters and an all-free-agent pool make the pool deeper than
  in a real league, for every arm alike.
- The as-of projection is a simple rate. Production projections (ESPN, Ours) are not reconstructed as of past weeks.
  The result speaks to the decision rule given a projection, not to the product's projections.
- Injuries are not projected ahead: only byes are known in advance, as in the planner, which uses only verified
  unavailability.

## Results

(Added after the run.)

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

**Pre-registered outcome: no evidence.** Against doing nothing, PLAN's stashes clearly help. Against the
projected-total greedy pick, there is no evidence either way.

### Run history (disclosed)

The first run (f29592b) deviated from the pre-registration. A player with no game before `w` took his team from his
**first game later this season**, and a player who never played that season was dropped as unprojected. That leaked
future teams into byes and made the planner abstain (`drop_unknown`) on 1,804 team-weeks across 164 of 360 teams.

An independent review caught it. 06eb923 restores the pre-registered as-of rule: with no game before `w`, the player's
team is the one from his last previous-season game, and every player with a prior season is projected.

The second run (06eb923) still had a smaller leak, which Codex caught on #347. The loader numbered players in the
order they first appeared in the data, starting with the evaluated season. The draft draws its seeded noise in that
order, so who played which weeks later in the season could reorder the draws and change the preseason rosters. The
pre-registration drafts on preseason value only. 9bba4f8 numbers each season's players from the previous season alone,
and a test checks that removing one later-season game leaves every roster the same.

Codex then found that positions had the same problem: a returning player took his position from his evaluated-season
games, so a later game listed at another position could change the draft's caps and his lineup eligibility. e9a85eb
takes a returning player's position from his last previous-season game, and a newcomer's from his first game, which
precedes any week he is projected at. On the real data this changed nothing: the e9a85eb run's rows are byte-identical
to the 9bba4f8 run's.

Codex also found that the post-hoc share of B1 picks that are *pick up now* checked membership in the planner's listed
top 5, which undercounts. 243c5fa applies the planner's own test to B1 directly. In the 243c5fa run only that flag
changed: 79% of B1's picks are pick-up-now, not 71%.

Each run uses the same rule, data and seeds. The outcome is "no evidence" in all three.

| | First run, f29592b (team leak) | Second run, 06eb923 (draft-order leak) | **Final run, 9bba4f8 = e9a85eb** |
|---|---:|---:|---:|
| Team-weeks | 3,960 | 3,960 | 3,960 |
| With a stash (compared) | 2,014 | 3,689 | **3,658** |
| Abstained (`drop_unknown`) | 1,804 | 0 | **0** |
| No stash found | 142 | 271 | **302** |
| `R_PLAN` | +13.5 | +13.8 | **+14.0** |
| `D1` | +0.7 | -0.2 | **-0.9** |
| Outcome | no evidence | no evidence | **no evidence** |

### Final run (9bba4f8, re-run at e9a85eb with identical rows, 2026-10-24)

| | Mean, realized points over the 4 later weeks | 95% CI, (season, week) clusters (33) | 95% CI, (season, league, team) clusters (360) |
|---|---:|---|---|
| `R_PLAN` (against NONE = 0) | **+14.0** | 9.6 to 18.2 | 13.0 to 15.0 |
| `D1 = R_PLAN - R_B1` (deciding) | -0.9 | -10.6 to 8.1 | -3.2 to 1.3 |
| `D2 = R_PLAN - R_B2` (reported) | +4.0 | -5.0 to 12.7 | 2.1 to 5.9 |

- **By season** (n, `R_PLAN`, `D1`, `D2`):

  | Season | n | `R_PLAN` | `D1` | `D2` |
  |---|---:|---:|---:|---:|
  | 2023 | 1,256 | 12.1 | +4.7 | +7.0 |
  | 2024 | 1,218 | 15.3 | +18.6 | +21.8 |
  | 2025 | 1,184 | 14.7 | -27.1 | -17.5 |

- **Precision.** 67% of PLAN's stashes realized a positive gain.
- **Projected against realized** (the pre-registered calibration). The means are close (+12.9 projected, +14.0
  realized), but the projection ranks stashes only weakly: the slope of realized on projected is 0.37 and the
  correlation 0.21. By quintile of projected later gain, projected → realized:

  | Quintile | 1 | 2 | 3 | 4 | 5 |
  |---|---:|---:|---:|---:|---:|
  | Projected | 1.5 | 4.2 | 8.5 | 16.2 | 33.9 |
  | Realized | 6.6 | 7.3 | 13.8 | 19.7 | 22.6 |

  Small projected gains are under-projected and large ones over-projected. **The stash gains are not calibrated**,
  and only the means match.
- **No-stash team-weeks.** On the 302 team-weeks where PLAN found no stash, B1 realized a mean of +10.4.

### Reading (post-hoc, does not change the outcome)

- **B1 is mostly not a stash.** 79% of B1's picks are players PLAN lists as *pick up now*: they add points this week,
  so the planner excludes them as stashes by design.
  - So the pre-registered arms compare a stash with a mostly current-week pick. The pre-registered Question asked for
    "a fair lineup-unaware pick of the same kind", and the Arms definition did not enforce it.
- **B1 has very few independent outcomes.**
  - It is the single free agent with the highest projected total. With static rosters and an all-free-agent pool,
    that is the same player for every team in a week: 1 distinct pick per week in 2023 and 2024, 1-3 in 2025, against
    78-80 distinct PLAN picks per season.
  - So `D1` mostly records whether one player broke out or busted. In 2024 B1 was mostly a backup QB whose prior-season
    rate came from one game (the prior has no minimum games). In 2025 it was an elite RB.
- **The intervals overstate precision.** The team clustering ignores that every team shares the same B1 player in a
  week, and adjacent weeks share realized weeks (the horizons overlap). The week-clustered intervals, from 33 clusters,
  are the honest ones.
- **`R_PLAN > 0` is not hindsight alone.** A random projected free agent with the same drop realizes -4.6 (week CI -5.3
  to -4.0). PLAN minus that random pick is +18.6 (week CI 14.5 to 22.6).

What the run supports: a stash chosen by the exact lineup difference beats doing nothing, and beats a random free agent,
on realized points. What it does not support: that the planner beats a good lineup-unaware pick, or that its projected
gain is calibrated. The product claims nothing beyond the v1 overview, and the stash page should not present the
projected gain as a forecast of realized points.

### Further limitations found in the run

- Drafting on prior-season totals takes retired players (for example, a 2022 quarterback who retired before 2023). Each
  is a roster spot that scores 0 all season, for every arm alike.
- nflverse labels some fullbacks FB (71-110 player-seasons a year). They are excluded, although ESPN lists some of them
  as RB.

### Suggested v2 (not run; would need its own pre-registration)

- A baseline of the same kind as the planner's pick: the highest-projected free agent who adds nothing this week. Or
  choose the baseline per team, for example at the position of the team's weakest bye-week starter.
- Leagues where claimed players leave the pool, so one breakout cannot be every team's pick.
- A minimum number of prior-season games for the prior rate.

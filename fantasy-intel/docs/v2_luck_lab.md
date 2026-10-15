# V2 Luck vs. Skill Lab (historical, explanatory, read-only)

Code: `backend/app/analytics/luck_lab.py` (service), `backend/app/api/luck_lab.py` (`GET /api/luck-lab`), the
`/luck-lab` page (nav "Luck lab"). Tests: `tests/test_luck_lab.py`.

The Lab separates three things that a fantasy record mixes together:

* how well a team scored each week;
* what its head-to-head results were;
* how strong the opponents were that it happened to face.

It answers:

* who scores well but loses;
* who has a better record than their scoring suggests;
* who faced the toughest opponents;
* what a team's record would be against another manager's opponents;
* which weeks made the gap.

It is **explanatory, not predictive**. There are no probabilities, no projections and no future expected wins.

## Source data audit

| Question | Answer from the stored data |
|---|---|
| How is a completed matchup identified? | ESPN `matchups.winner` is HOME, AWAY or TIE. `UNDECIDED` (or anything else) is in progress or future, and is never a result, even when live points are stored. This is the same rule the Playoff Race Lab uses (`playoff_race.FINAL_WINNERS`). |
| Is each team's score available? | Yes. `home_points` / `away_points` are ESPN's `totalPoints` for the matchup period. A NULL score is unknown; 0.0 is a real score. |
| Matchup periods vs NFL weeks | A matchup period can span several scoring periods (`scheduleSettings.matchupPeriods`, read with `playoff_race.period_weeks`). Scores are period totals, so a two-week matchup is compared only with other two-week totals in the same period. |
| Ties | `winner = TIE`. A tie counts half a win. If ESPN broke an equal-points matchup with a league tiebreak, the result is ESPN's. |
| Byes and missing opponents | A row with one team listed. Such a period is not fully observed (see below). |
| Playoff games | `playoffTierType` other than `NONE`, or a period past `reg_season_matchups` (`playoff_race.is_regular`). They are never mixed with the regular season. |
| Teams with reliable scores per period | Reported per excluded period (`teams_with_final_scores` / `teams`). |
| Corrections and duplicates | The database keys matchups by (league, season, matchup id), so a later sync overwrites a row (ESPN corrections replace older values) and true duplicates cannot be stored. The service still guards its input: an identical repeated matchup id is counted once; conflicting rows with the same id make their period ineligible; rows without an id are used but counted; a team appearing in two matchups of one period makes that period ineligible. |

## Eligible periods

A regular-season matchup period is **eligible** only when it is fully observed. Every league team must appear in
exactly one completed two-team matchup, with both scores present.

Any other period is excluded for **every** team, so no team's expected wins are computed against a smaller pool.
Excluded periods are listed with their reasons and a kind:

* `not_completed`: nothing final yet, as with the current or a future period;
* `incomplete`: a data gap, such as a missing score, a bye row, a missing team, duplicates or an unknown team.

If no period is eligible, the page says so and shows no luck numbers.

## Formulas

All are computed over eligible periods only, and actual and expected are always compared over the same periods.

* **Expected wins** for a period:

  > (teams outscored + 0.5 × teams tied) ÷ (teams − 1)

  The score is compared with every other team's score in that period, using
  `playoff_race.period_all_play`, the same pairwise helper as the Playoff Race Lab's all-play. Season expected
  wins is the sum over eligible periods. For example, in a 12-team league, outscoring 9, tying 1 and losing to 1
  gives (9 + 0.5) / 11 = 0.864. This is an all-play win equivalent, not the probability of winning the actual
  matchup.
* **Actual win equivalents** = wins + 0.5 × ties.
* **Schedule luck** = actual win equivalents − expected wins.
  * Positive means more actual wins than the weekly scoring rank implies. Across a league it sums to zero.
  * It is an observed result gap. It is not proof of luck or skill, and not a prediction of regression.
* **All-play record**: summed pairwise W/L/T. On fully observed periods it equals the Playoff Race Lab's all-play
  (regression-tested).
* **Weekly rank**: 1 = highest score; equal scores share the better rank.
* **Bands**: top band = the `b` highest scores and bottom band = the `b` lowest, where `b` = 3, or half the league
  when it has fewer than six teams (so the two bands never overlap). These are descriptive, not predictive
  thresholds.
* **Notable results:**
  * *Brutal losses*: a loss as a top-band scorer, a loss with a score above the period median, or a loss to the
    period's top scorer.
  * *Fortunate wins*: a win as a bottom-band scorer, or a win with a score below the median.
  * Each is shown with exact points, the weekly rank, the opponent's rank and the margin.
* **Consistency and scoring strength:**
  * mean, median, minimum, maximum and range;
  * Q1 / Q3 / IQR (Python `statistics.quantiles(n=4, method="inclusive")`; needs at least two periods);
  * share of periods in the top half (rank ≤ teams / 2);
  * all-play win percentage;
  * first-place weeks (nobody scored more) and last-place weeks (nobody scored less), with ties sharing either.
  
  These are never combined into a single "skill score".
* **Schedule difficulty** (opponents actually faced):
  * total and mean opponent points;
  * mean opponent all-play share in the weeks you met them, i.e. how good the opponent's score was that week
    relative to the league;
  * mean of those opponents' season-long all-play share, i.e. how strong those managers were overall. Comparing
    the two separates an opponent's week from their season.
  * how many opponent weeks were in the top or bottom band;
  * the hardest and easiest opponent scores faced.
  
  The difficulty rank orders teams by mean opponent points (1 = toughest).

## Counterfactual schedules

For team A and another manager B, for each eligible period:

1. Keep A's actual score.
2. Take the opponent B actually faced in that period, and that opponent's actual score.
3. The hypothetical result compares points (W / L / T); ESPN tiebreaks are not applied.

If B's opponent that period is A itself, the period is **excluded** and no result is invented. A's actual record
is recomputed over exactly the remaining periods, so actual and counterfactual are always comparable.

Reported:

* the periods compared and those excluded, with the reason;
* actual and counterfactual W/L/T and win equivalents;
* the difference;
* the flipped periods (L→W, W→L, and changes involving ties);
* every row, with exact scores and the substituted opponent.

The **all schedules** table shows A under every other manager's schedule. Each row covers its own periods (B's
games against A are excluded), so rows are listed by team rather than ranked, with the number of periods and
"same periods as all eligible" shown. Win percentages compare like with like within a row.

These are hypothetical opponent assignments, **not** reconstructed valid league schedules. In a real alternative
schedule every team's opponents would change together. Lineup decisions are taken as they happened; no
hypothetical optimized past lineups are built.

## API and page

`GET /api/luck-lab?team_id=&schedule_of=` (`team_id` defaults to your `is_mine` team) returns:

* `coverage` (eligible and excluded periods, data notes);
* `leaderboard`;
* `summaries`, one per team;
* `spotlight`;
* `timeline` (per period: score, opponent score, result, expected wins, luck, cumulative luck);
* `biggest_contributors`;
* `counterfactual` for the chosen `schedule_of`;
* `all_schedules`;
* `matrix` (per team and period: rank, result, expected wins, flags);
* `rules` and `notes`.

It is read-only. An unknown team returns 404, choosing your own schedule returns 422, and no synced league
returns 503.

The page has:

* team and schedule selectors;
* the coverage line, plus a callout for data gaps;
* your team's spotlight (record, expected record, luck, opponents faced, biggest brutal loss and fortunate win,
  consistency, opponent context);
* the league leaderboard with luck bars;
* an actual-vs-expected scatter plot (above the diagonal = more wins than scoring implies);
* the week-by-week score chart with W/L/T letters;
* the per-period luck and cumulative-luck chart (one scale, in wins);
* the counterfactual explorer and the all-schedules table;
* the weekly results matrix (rank and result in each cell, shaded by all-play share, ⚑ for notable results).

Colour is never the only signal: results are letters, ranks are numbers, and luck is signed. The page is usable at
phone width; this PR also lets the global nav wrap on narrow screens.

## Performance

The calculation is a straightforward exact one, done once per request and shared across every team and schedule:
about 2–3 ms for 10–14 teams over 14 periods, with a 60–80 KB payload.

## Validation and limitations

Synthetic only. No local ESPN database exists in the development environment, so **nothing was validated against
real Los Subies data**. ESPN's real stored `matchups` rows need checking for points coverage per period, ties,
byes, playoff tiers and post-period corrections.

* Expected wins assume every other team's score was a possible opponent that week. That is the definition of
  all-play, not a model of the real schedule.
* A period with any missing team or score is excluded for everyone. In leagues with byes (odd team counts) no
  period may qualify.
* Counterfactual records swap one team's opponents only. They are not valid alternative league schedules, and
  hypothetical ties use points without ESPN tiebreaks.
* Results are ESPN's as stored. Stat corrections after the last sync are not reflected.

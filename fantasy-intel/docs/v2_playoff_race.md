# V2 Playoff Race Lab (read-only)

Code: `backend/app/analytics/playoff_race.py` (service), `backend/app/api/playoff_race.py`
(`GET /api/playoff-race`), the `/playoff-race` page. Tests: `tests/test_playoff_race.py` (controlled synthetic
leagues in `tests/synthetic_race.py`, plus the ESPN fixture for the database and API tests).

It shows:

* the league's actual standings, all-play record and points;
* every remaining regular-season matchup with both sides' ESPN-projected optimal lineup totals;
* one deterministic projected scenario (the higher projection wins);
* the playoff cutline, the bubble and high-leverage games;
* paths for a spotlight team (your `is_mine` team by default);
* winner overrides that you control through the URL.

It writes nothing, and nothing in it feeds projections, trade values, trade discovery, waivers, player signals or
Schedule Intelligence.

## Source-data audit

| need | stored source | verified meaning / gap |
|---|---|---|
| Actual record, PF, PA | `fantasy_teams.wins/losses/ties/points_for/points_against` (ESPN `record.overall`) | ESPN's own record. It is the starting point and is cross-checked against the stored final matchups; any mismatch is listed (`record_check`) and ESPN's record is used. A missing ESPN record is derived from the final matchups, and that is said. |
| Current seed | `fantasy_teams.playoff_seed` (ESPN `playoffSeed`) | Shown as ESPN reports it. If it disagrees with record order (divisions or seeding rules), a warning is shown. |
| Matchups, past and future | `matchups` (ESPN `schedule`: `matchupPeriodId`, home/away `teamId` and `totalPoints`, `winner`, `playoffTierType`) | **Final only when `winner` is HOME, AWAY or TIE.** For a matchup in progress, `totalPoints` is the live total as of the sync and is never used as a result. A row with one team (a bye or an unknown opponent) produces no result. A matchup sent without a matchup period is left out of all-play, schedules and scenarios and reported; if it is a regular-season final, its result still counts when deriving or cross-checking records. The ESPN schedule is not assumed complete: per-team coverage of every regular-season period is checked and gaps are reported. |
| Regular season vs playoffs | `playoffTierType` (`NONE` = regular), `leagues.reg_season_matchups` (`matchupPeriodCount`) | A matchup with a non-`NONE` tier, or a period past the regular-season count, is a playoff matchup. Playoff matchups are shown as ESPN lists them; **future playoff opponents are not modeled**. |
| Matchup period to scoring periods | `settings_raw.scheduleSettings.matchupPeriods` (fallback: period = week when `matchupPeriodLength` is 1 or unreported) | Multi-week matchups are supported. The last regular-season week comes from `projections.last_regular_season_week`. A period that cannot be mapped is `unmapped`: never projected, and listed. |
| Playoff spots | `leagues.playoff_teams` (`playoffTeamCount`) | Used for the cutline. If it is missing, there is no cutline, and a warning is shown. |
| Divisions | `fantasy_teams.division_id` | Counted. Division-based spots or seeding are **not modeled**; with more than one division a warning is shown. |
| Seeding / tie rules | `scheduleSettings.playoffSeedingRule(By)`, `scoringSettings.matchupTieRule(By)`, `playoffMatchupTieRule` | Reported raw when present. Their vocabulary is not verified in this codebase, so **none is applied**. |
| Projections | `espn_player_weekly_points.projected_points` through `trade_data.load_league_state` | Unmodified ESPN weekly projections and current rosters (`lineup_snapshots` at ESPN's current week). |
| Weekly lineups | `team_lineups.project_team` | The canonical optimizer: each scoring period is optimized separately with the league's slots, and current-IR players are inactive. Missing is not zero. |
| Current week | `trade_data.evaluation_start_week` | Kickoff-aware: a scoring period that has kicked off is never projected. |

## Rules

* **Actual vs forward.** Actual records, points for/against and all-play come only from ESPN results. Projected
  records add hypothetical results to ESPN's record. Points for and against never include projections.
* **All-play.** For each final regular-season period, every scored team is compared with every other scored team
  that period (win, loss or tie). Periods with fewer than two scored teams are skipped, and the periods used are
  listed.
* **Projected matchup total** = the sum of the side's optimized weekly totals over the matchup's scoring periods
  (`project_team`, unchanged).
  * A side is incomplete (projected total NULL, known-only total shown) when any of its weeks is incomplete: a
    lineup-relevant rostered player with an NFL team and no ESPN projection.
  * A team with **no roster snapshot** is a gap. It is never projected as 0, which the optimizer would otherwise
    return for an empty roster.
  * A projected 0 (for example a bye) is a real value.
* **Which matchups are projected.** Only `upcoming` matchups: every scoring period at or after the first actionable
  week. A matchup with any period already kicked off (including a multi-week matchup half played) is `in_progress`,
  shows ESPN's live points labelled "not final", and stays **unresolved** unless you override it.
* **Projected winner** = the higher projected side, only when both sides are complete. Equal totals (to 0.01) have
  no projected winner.
* **Close matchup** (a potential swing game): the projected margin is at most 5% of the two sides' mean projected
  total. This is a display threshold, not a probability.
* **Standings order** = winning percentage, (wins + half of ties) / games.
  * This is the usual ESPN head-to-head order, but it is not read from a league setting.
  * Equal percentages share a rank ("=") because **no tiebreak is applied**.
  * Undecided games are counted per team ("+n?"); standings count decided games only.
* **Cutline status** with P playoff spots:
  * *inside* when the teams strictly ahead, plus the team's own tie group, fit in P;
  * *outside* when P teams are strictly ahead;
  * otherwise *tied at the cutline*: unresolved, because no tiebreak is applied.
* **Boundary teams are whole rank groups.** "Last in" is the lowest-ranked group fully inside and "first out" the
  highest-ranked group fully outside. A group straddling the last spot is reported as a cutline tie, with the number
  of spots its members would share, and no member of it is named in or out. On the page, the dashed cutline is drawn
  above such a group.
* **No clinch or elimination** is ever claimed. That would need the full seeding, tiebreak and division rules,
  which are not verifiably available. Win-out and lose-out records are shown instead.

## Scenario explorer

* Baseline:
  * `base=projected` (default): the higher ESPN projection wins every eligible upcoming game;
  * `base=actual`: no projected results.
* Overrides: `override=MATCHUP_ID:home|away|tie|none`, repeatable.
  * `none` forces a game unresolved.
  * Only regular-season games that are not final, and that list two teams, can be overridden: in progress,
    upcoming or unmapped. Anything else returns 422.
  * The page keeps overrides in the URL (`o=` parameters), so every change is reversible with the back button,
    the per-override ✕ or "clear all".
* Answers it supports:
  * *What if I win next week?* Override your next game.
  * *What if a bubble team loses?* Override its game.
  * *What if two results flip?* Two overrides.
  * *How many wins would I have?* The spotlight's scenario record.
* **Leverage.** For each team's open games: its rank and cutline status if it wins and if it loses, with every other
  result as in the scenario. *High leverage* means the two statuses differ. Bubble teams are teams with a
  high-leverage game, plus any team tied at the cutline.
* **Paths for the spotlight team:**
  * scenario, win-out and lose-out records and statuses;
  * the **fewest own wins** among its open games that put it inside, with every other result as in the scenario.
    The search is exhaustive up to 12 open games and skipped (and said so) above that; ties between sets prefer
    games the scenario already gives it, then the closest projected margins;
  * results elsewhere whose flip improves its rank or status, each with an "apply" link.

## What-if (issue #192)

A what-if is the scenario with at least one result set by you (`override=`). With no result set the answer is
exactly the scenario-off answer (the default goldens are unchanged); with one, it adds a `whatif` block:

* `inputs`: the baseline (`projected` or `actual`), the first projectable week and every set result with the
  baseline result it replaced (`changes_baseline` false when you set what the baseline already had).
* `assumptions` and `method`, and `sampling`: `deterministic`, 1 exact scenario, 0 random samples, no seed and no
  probability. Probabilistic season simulation is separate, calibration-gated work and is not done here.
* `completeness`: `partial` when any remaining game is unresolved, with counts per reason (`projection_incomplete`,
  `equal_projection`, `in_progress`, `unmapped`, `actual_baseline`, `set_unresolved`, and `no_period` for an undecided
  regular-season game sent without a matchup period, which can be neither placed nor set). Unresolved games count for
  nobody; a missing projection never decides a game and is never scored as 0.
* `baseline_standings` (the same baseline without your results) and `changes` (each team's baseline and what-if
  rank, record and status, the rank change and whether the status changed). Both are labelled hypothetical; the
  actual standings are `scenario.current_standings`.
* `conclusions`: a gate. `blocked_by` names every rule a clinch or elimination conclusion would need that is not
  mapped: the tiebreak and seeding settings (`MAPPED_TIEBREAK_RULES` is empty, so any reported value stays
  unmapped and is listed raw), the standings order (assumed), divisions (more than one: not modeled), seeds that
  disagree with record order, and unknown playoff spots. `claims` is always empty.
* `conclusions.record_bounds`: each team's record if it wins, and if it loses, every open game, with your set results
  fixed and every other remaining game open (projections are not used). `record_order` is `above_line` when, even
  losing out, fewer other teams than there are spots can match its winning percentage; `below_line` when, even winning
  out, at least that many finish strictly ahead in every outcome; otherwise `open`. It is never turned into a claim
  (`claim` is always null). It is exact (tests check it against every outcome) only under these conditions; when any
  fails, the bounds abstain (`record_order` and the counts null, each reason listed in `abstain`; the records
  themselves are still shown):
  * the assumed winning-percentage order decides the spots: one division, and the provider's seeds follow record
    order;
  * every team's record is known and matches the stored final matchups (`record_check` is empty), so the remaining
    schedule explains every future result;
  * no median game: a Sleeper league with `league_average_match` adds a result against the league median each week;
  * the schedule is complete: the regular-season length is known, no team is missing a regular-season matchup, and no
    undecided matchup lacks a period.

The page shows the what-if section only when a result is set: the method line with the sample count, the set
results (each with a link to the Matchup page for its week and a remove link), a partial notice, the baseline vs
what-if standings table, the blocked conclusions, the record bounds ("Not claimed"), the assumptions, a "Copy
scenario link" button (the league-scoped URL with every set result) and "Reset to the baseline". Every standing on
the page is labelled actual or hypothetical.

## Rooting guide (issue #217)

`scenario.rooting` answers "which other games this week do I want to go one way?" for the spotlight team, from the
same deterministic evaluator as the what-if. It is present whenever there is a spotlight team, with or without set
results, and it needs no new parameter.

* **Which games.** Every open regular-season game of the next matchup period (the earliest period with an open
  game, under way included) that the spotlight team does not play. Its own game is named (`own_game`) and left to
  the leverage table. Final games, byes and later periods are not evaluated; `later_open_games` counts the later
  ones that can still change everything.
* **One game at a time.** `evaluate_outcome` sets one game's result (home win, then away win) without changing the
  scenario or the fixtures, and reports two things:
  * `scenario`: the spotlight team's rank, tie flag, cutline status and record, every other result as in the
    scenario (hypothetical; the projected baseline plus your set results).
  * `range`: its record bounds with that result fixed (plus your set results) and every other game open, with no
    projections. It is null when the bounds abstain (`range_abstain` gives the reasons, the same as the what-if's).
* **Which side.** `prefer` names a side only when it is no worse on both comparisons and better on at least one.
  The scenario compares cutline status, then rank. The range compares record order only (above the line in every
  outcome, open, below it in every outcome). The team counts are reported but never decide: a team locked ahead of
  you can be irrelevant to your spot. When the two comparisons point to different sides, the game is a `conflict`
  and no side is named. `basis` lists which comparison decided.
* **Order.** Games with a preference come first, then cutline status swings, then the size of the rank swing, then
  record-order swings. Games where neither side helps are listed apart (`no_effect`).
* **Never a claim.** `probabilities` is null, `conclusions.claims` is always `[]`, and the conclusion gate's status
  is carried as-is. The page words the range as counts ("fewer teams than there are spots can match your record
  even if you lose every open game") and says "record order assumed; not a clinch or elimination". Ties are not
  compared.

The page shows a "Rooting guide: wk N" section labelled Hypothetical. It has one row per game: the side to root for
(or Mixed), the spotlight team's place either way, the reason in words from `rootingReason()`
(`lib/playoff-view.ts`), and links that set the result as a what-if. Every row reproduces the deterministic
simulation: `test_every_rooting_outcome_reproduces_the_deterministic_simulation` sets each result as an override
and checks the standings and the what-if's record bounds against the row.

Cost: about 13 ms more for a 14-team league (two standings and two bounds evaluations per game). The block is about
5 KB.

## Page

`/playoff-race` (nav "Playoff race") has these sections:

* scenario controls (baseline, active overrides, spotlight team);
* spotlight tiles (now / scenario / win out / lose out);
* the spotlight team's high-leverage games and helpful results elsewhere;
* current vs projected standings with movement arrows and the dashed cutline;
* a cutline ladder (scenario winning % bars, your team highlighted);
* a projected-margin chart for the spotlight team's remaining matchups (blue ahead, orange behind, "gap" when
  unresolved);
* bubble-team cards;
* every remaining matchup with both projections, a margin bar, a close badge, the scenario result and override
  buttons;
* the power view (actual record, PF, PA, all-play, projected regular-season starting points, remaining-schedule
  badges, coverage);
* playoff matchups ESPN already lists;
* the method and its limits.

The two chart colours (`#4a96ec` / `#d9762f`) were validated against the dark panel surface (`#151b23`) for
lightness band, chroma, colour-vision-deficiency separation and contrast.

## Validation and performance

Synthetic only. No local ESPN database exists in the development environment, so **nothing was validated against
real Los Subies data**, and synthetic tests say nothing about predictive accuracy.

Latency on synthetic leagues:

* about 6 ms for 6 teams and 8 periods;
* 0.4 to 0.7 s in the worst case of 12 teams with 12 open games for the spotlight team, where the fewest-wins
  search is exhaustive;
* the payload is about 150 KB.

## Limitations

* The projected scenario is deterministic: the higher ESPN projection "wins". It is not a probability, and close
  games are flagged rather than weighted.
* Rosters are ESPN's current snapshot for every remaining week. No waiver, trade or lineup change is modeled, and
  injured players follow ESPN's projections and the IR rule.
* No tiebreaks, division rules, clinching or elimination. A tie across the cutline stays unresolved. The what-if's
  record bounds are reported as counts behind a gate that keeps every clinch or elimination claim blocked.
* Standings ordering by winning percentage is assumed, not read from a setting. If ESPN's current seeds disagree
  with it, a warning is shown.
* Live points of a matchup in progress are ESPN's totals as of the last sync and never count as a result.
* Future playoff matchups and opponents are not modeled. Byes or one-team rows in the regular season produce no
  result.
* The fewest-wins search is exhaustive only up to 12 open games.
* The rooting guide evaluates one game at a time, for the next period only. Combinations of results and later weeks
  are left to the what-if. A preference is about this scenario and the record order; it is not a chance.

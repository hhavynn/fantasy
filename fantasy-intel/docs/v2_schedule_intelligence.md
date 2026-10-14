# V2 schedule intelligence (Phase A: descriptive)

Code: `backend/app/analytics/schedule_intelligence.py` (read-only service), `backend/app/api/schedule_intelligence.py`
(`GET /api/schedule-intelligence`), the `/schedule-intelligence` page. Tests: `tests/test_schedule_intelligence.py`
(controlled synthetic inputs in `tests/synthetic_nfl.py`). Specification: `docs/research/v2_schedule_intelligence.md`.

For each QB/RB/WR/TE on a fantasy roster and each remaining scoring period: the player's current NFL team, the
scheduled opponent, home/away or bye, regular season or fantasy playoffs, and what that opponent's defense has
allowed in games completed **before** that week, with numerators, denominators, games sampled, ranks and
explicit reasons when evidence is missing or too thin. It is context, not a projection: no number here enters ESPN
projections, player scores, BUY/HOLD/SELL, waivers, manager scouting, the trade evaluator or trade discovery.

## Source-data audit (existing tables; no new ingestion, no schema change)

`nfl_defense_week` is built in `nfl/sync.py::derive_pbp` from nflverse REG play-by-play, one row per defense and
week, restricted to `scrimmage` plays (`play_type` run or pass, not a two-point try):

| column | exact meaning (verified in code) | finding |
|---|---|---|
| `pass_plays` | rows with nflverse `pass` = 1 **and EPA present** | nflverse `pass` covers pass attempts, sacks and QB scrambles; penalty no-plays (`play_type` `no_play`) and two-point tries are excluded. It is the nflverse pass-play definition, close to but **not verified identical to `qb_dropback`**, so it is labelled "eligible pass play", never "dropback". |
| `pass_epa_sum` | sum of EPA over the same rows | consistent with `pass_plays` (null EPA excluded from both) |
| `rush_plays` | designed rushes (run with a rusher, not a scramble, not a kneel, not 2-pt) **with EPA** | |
| `rush_epa_sum` / `rush_success` | EPA sum / count of nflverse `success` = 1 (EPA > 0) over designed rushes | consistent with `rush_plays` |
| `explosive_runs` | designed rushes with >= 10 yards, **including rows without EPA** | denominator mismatch with `rush_plays` (used by `defense_context.explosive_run_rate_allowed`); not used here |
| `targets` | pass attempts with a receiver, not a sack, not 2-pt | |
| `rb/wr/te_targets` | targets whose receiver's nflverse position is RB (FB counted as RB) / WR / TE | position = the receiver's **latest** weekly-roster position this season (else the players table), not the position in that week; targets to other or unidentified receivers count only in `targets` (unknown and "other" cannot be told apart) |

Not available from the stored aggregates: **passing success rate allowed** (no pass success column), pressure,
coverage scheme, alignment, cornerback assignments, routes. They are not shown.

`defense_context` is a season-to-date snapshot (`through_week` = the latest week loaded). It is **not** reused: it
would leak later games into any earlier week, and it counts every game with play-by-play rows, including one that
was still in progress at sync time (nothing in `derive_pbp` checks completion). The existing next-week matchup label
in `player_context` keeps using it unchanged.

## No lookahead and completed games only

For a matchup in week W, the opponent's sample is its `nfl_defense_week` rows of the same season with week < W
whose game is **verified complete**. Every defense is ranked under the same cutoff, so ranks for week W use only
pre-W games league-wide. Other seasons never enter.

**What counts as complete.** The nflverse schedule dictionary defines `home_score` / `away_score` / `result` only as
"NA for games which haven't yet been played"; it does not say whether a score can be a live, in-progress score, so a
score alone is not treated as proof of a final (a 10-7 could be mid-game). A game is complete only when:

1. both scores are present (a shutout or a 0-0 is a real final once the timing rule holds), and
2. its scheduled kickoff (`gameday` + `gametime`, US Eastern, converted to UTC) is known, and
3. both inputs were fetched at least **8 hours after kickoff**: the schedule and the defensive play-by-play
   aggregates. Each fetch time is the recorded start (`sync_runs.started_at`, logged right after the nflverse
   downloads finish, minus a 1-hour margin for the download) of the latest successful NFL sync **for the league's
   season** whose stats show it wrote that table (`nfl_schedules`, `nfl_defense_week`). A sync can succeed with the
   play-by-play download failed, leaving older defensive rows in place; those rows keep their own, earlier fetch
   time. The earlier of the two times decides. No NFL game, including overtime and weather delays, lasts 8 hours,
   so a score and rows fetched that late can only describe the finished game.

The rule therefore judges the stored defensive rows as of the moment they were actually fetched. Everything else is unverified and excluded, with its reason listed per matchup
(`excluded_unverified_reasons`): no score yet; score present but fetched too soon after kickoff (possibly live);
kickoff or fetch time unknown; game not in the schedule. A game is picked up after the next NFL sync once the rule
holds. The response reports the fetch time, the rule, and how many scored games are still unverified.

## Metrics by position

All are sums over the sampled games (never a mean of per-game rates). Rank 1 = most permissive (the highest value
allowed to offenses), among defenses with >= 3 completed sampled games (`MIN_DEF_GAMES`, the existing app constant).

| metric | positions | numerator / denominator |
|---|---|---|
| EPA allowed per eligible pass play | QB (primary), WR (primary), TE (primary) | `pass_epa_sum` / `pass_plays` |
| EPA allowed per designed rush | RB (primary) | `rush_epa_sum` / `rush_plays` |
| Designed-rush success rate allowed | RB | `rush_success` / `rush_plays` |
| Share of targets to RBs / WRs / TEs | RB / WR / TE | `rb_targets` / `targets` (etc.), plus position coverage `(rb + wr + te) / targets` |

Target shares are observed tendencies of what offenses did against the defense; they are not proof of a "funnel"
and say nothing about one player's targets.

## Labels (descriptive, documented, not predictive)

The tougher/permissive band uses only the position's primary metric and the same thresholds as the existing
next-week matchup label (`analytics/context.py`): ranks 1-10 "observed more permissive", the 10 lowest ranks
"observed tougher", otherwise "middle". "Insufficient evidence" when the opponent has fewer than 3 sampled games, the
metric has no eligible plays or a needed column is missing, or fewer than 21 defenses can be ranked (the two bands
would touch). "Bye" when the team has no game that week; "unavailable" when the NFL schedule does not cover the week
or the player has no current NFL team. There is no composite schedule score: window summaries only count weeks per
label.

## Missing versus zero

A metric with no eligible denominator is NULL with a reason ("no eligible rush plays in the sample"); a game whose
needed column is NULL makes that metric NULL ("pass_plays/pass_epa_sum missing for 1 game(s)"), never 0. A real
observed 0 (e.g. no successful designed rushes in 175 attempts) is 0. NULL ranks stay unranked.

## Weeks and windows

Scoring periods come from the league (ESPN `current_week` .. `final_scoring_period`); the regular-season/playoff
boundary and multi-week playoff matchups come from ESPN `scheduleSettings` (`last_regular_season_week`, matchup
period per scoring period). Nothing is hard-coded to weeks 15-17. ESPN NFL scoring periods are NFL weeks (see
`docs/v2_trade_evaluator.md`). The first actionable week is the trade evaluator's kickoff-aware rule; a current week
already under way is shown as informational (`actionable: false`). Windows: next actionable week, remaining regular
season, fantasy playoffs, all remaining. Player teams are the current ESPN snapshot for every remaining week (no
prediction of trades or releases). A player whose latest nflverse weekly-roster team differs from ESPN's, or whose
ESPN-to-GSIS mapping is not high-confidence, carries a note; the schedule always uses ESPN's team.

## API

`GET /api/schedule-intelligence?team_id=&position=all|QB|RB|WR|TE&phase=all|regular|playoffs&player_id=`

* `team_id` defaults to the `is_mine` team; `player_id` returns any one skill-position player (also a free agent).
* Read-only connection; no writes, syncs or ESPN calls. 404 unknown team/player, 422 bad filter, 503 no league.
* Response: league, team(s), the window (first actionable week, playoff weeks, matchup periods, uncertainty),
  schedule coverage, data freshness (last NFL sync, defensive weeks loaded), per-player weeks with evidence and
  summaries, metric definitions, the label rule and notes. Deterministic for the same database and clock.

## Page

`/schedule-intelligence` (nav "Schedule"): filters for team (default yours), position, regular season/playoffs and
player; an overview table (next actionable matchup with its evidence, regular-season and playoff opponents as
colour-coded chips with the opponent's rank, coverage); a fantasy-playoff section; a player drilldown with every
week's metrics, numerators/denominators, ranks, games sampled and excluded in-progress weeks; definitions and
limitations. The player page links to it.

## Validation and performance

Synthetic only: no local ESPN/nflverse database exists in the development environment, so nothing was validated
against real 2026 Los Subies data. The tests use controlled synthetic defensive values (not real NFL outcomes).
Latency on the synthetic league (32 teams, 18 weeks, defensive weeks 1-8): ~45 ms for a 10-player roster, ~90 ms
for 234 players; the payload for a 10-player roster is ~140 KB.

## Limitations

* Descriptive only. Allowed EPA and target shares mix defense quality with the offenses and game scripts faced; no
  opponent adjustment, shrinkage or recency weighting (Phase B research, `docs/research/v2_schedule_intelligence.md`).
* Receiver positions are latest-season positions, not week-specific; "other" and "unidentified" receivers cannot be
  separated.
* A sync during a game stores partial rows; they are excluded until a sync at least 8 hours after kickoff.
* With no successful NFL sync recorded (unknown fetch time) no game can be verified, so no defensive evidence is shown.
* Explosive-run rate is not shown because its stored numerator and denominator use different filters.

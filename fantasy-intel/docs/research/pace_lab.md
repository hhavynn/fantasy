# Play-pace and game-script matchup lab (pace-0.1.0, research only)

Date: 2026-10-23. Issue #211. Status: **research only.** Nothing here is promoted, read by a projection or forecast,
or changes any production model, run, ledger, registry or artifact. Code: `backend/app/analytics/pace_lab.py` (the
lab, served read-only at `GET /api/pace-lab`), `backend/app/analytics/pace_backtest.py` (the frozen backtest, never
imported by the app), `scripts/pace_lab_backtest.py`, tests `backend/tests/test_pace_lab.py`. Page: `/pace-lab`.

> **Status of this file.** Sections 1-4 (data inventory, definitions, the lab's model, the preregistered backtest)
> were committed *before* the backtest was run on real data. Section 5 (results) is added afterwards in a later
> commit and never edits the preregistration block. Every result records `pace_backtest.spec_hash()`; a changed
> spec is a new version, not a rerun.

## 1. Data inventory (what is stored, what is not)

The lab reads only tables the NFL sync (`app/nfl/sync.py`) and the historical ingestion (`app/nfl/history.py`) already
write from nflverse play-by-play and schedules. No new source, scraper, paid API or market data is used.

| Table | Grain | Used fields | Derivation |
|---|---|---|---|
| `nfl_team_week` | season, week, offense | `offense_plays`, `dropbacks`, `team_designed_rushes`, `team_targets`, `neutral_plays`, `neutral_pass_rate`, `proe`, `proe_plays`, `opponent` | `sync.derive_pbp` (regular season; two-point tries excluded; neutral = 0.20 <= wp <= 0.80 and > 120 s left in the half) |
| `nfl_team_game_drives` | game, offense | `drives`, `plays` | `kicking_pbp.derive_team_drives` (nflverse `fixed_drive`; a drive needs >= 1 scrimmage play) |
| `nfl_schedules` | game | `home_team`, `away_team`, `home_score`, `away_score`, `overtime`, `gameday`, `game_type` | raw nflverse schedules |

**Never read:** `spread_line`, `total_line`, moneylines, odds and `team_game_lines` (betting markets). A test scans
the module for these names.

**Gaps (raw play-by-play is not stored, so these cannot be computed from what is):**

* **Seconds per play.** No per-snap game clock is stored. Pace is measured as plays per game, drives per game and
  plays per drive. Adding seconds per play would need a new play-by-play derivation in the sync, which this issue
  deliberately does not change.
* **Play-level score state.** Run/pass by the score at the snap (leading / trailing at each play) is not stored.
  The lab classifies each *game* by its final margin instead. A final margin is outcome-based: it is known only after
  the game, and it is partly caused by the plays themselves (a team that runs late because it leads). Script
  tendencies therefore describe games that ended that way; they are not causal script effects.
* **Overtime length.** Only the overtime flag is stored, so overtime games are excluded from per-game volume instead
  of being rescaled to 60 minutes.

## 2. Definitions

* **Plays**: run/pass plays, `nfl_team_week.offense_plays` (dropbacks incl. sacks and scrambles + designed runs;
  kneels, spikes, two-point tries and penalty-only snaps excluded).
* **Pass rate**: dropbacks / (dropbacks + designed runs).
* **Neutral pass rate**: nflverse `pass` over neutral plays; a game with no neutral play has no rate (missing, never
  0%), and pooled rates weight each game by its neutral plays.
* **Drives**: drives with at least one scrimmage play. A no-play possession (a kickoff returned for a touchdown, a
  penalty-only possession) is not a drive; a game with no drive row is missing, never 0 drives.
* **Game script** (per game, from the final margin): competitive within 8 points, leading when won by 9+, trailing
  when lost by 9+.
* **Overtime**: excluded from per-game volume (plays, drives per game and per-script plays), kept in per-play rates.
  When the flag is not stored, overtime is unknown, the game is counted and the count is reported.
* **Final**: both scores stored and, when a sync time is recorded, a game date before that sync. A team-week row
  without a final schedule score is not counted. A final schedule game with no team-week row is *missing
  play-by-play*: listed in the team's table and counted as missing, never as zero plays.
* **Franchise moves**: every source is mapped to one code per franchise before any join (OAK -> LV, SD -> LAC,
  STL/LAR -> LA, JAC -> JAX, WSH -> WAS), so a pooled 2019-2020 view reads Oakland and Las Vegas as one offense, and
  the page names the codes it merged.

## 3. The lab's estimates

Every estimate is `(team total + k x prior) / (team sample + k)` with a prior strength fixed in advance (not fitted):

| Quantity | Prior | k |
|---|---|---|
| Plays per game, drives per game | league mean in the same filter | 4 games |
| Plays per drive | league rate | 60 plays |
| Neutral pass rate | league rate | 150 neutral plays |
| Pass rate | league rate | 120 plays |
| Targets per dropback | league rate | 100 dropbacks |
| Plays per game in one script | team's overall estimate + (league script mean - league mean) | 3 games |
| Pass rate in one script | team's overall estimate + (league script rate - league rate) | 120 plays |

Each estimate is returned with its observed value, sample size, prior, prior strength, data weight `n / (n + k)` and
counts of missing and excluded games. Filters: season (newest stored by default), 1-3 pooled seasons, team, opponent
(the team's games against it), window (season, last 8, last 4 games), and a point-in-time `through_week`.

**Hypothetical scenario.** The user sets a competitive / leading / trailing mix. Central estimate of plays =
sum over scripts of weight x the team's per-script plays estimate; dropbacks, designed runs and targets multiply by
the per-script pass rate (and targets per dropback). The range is the 10th-90th percentile of the mixture of each
script's league-wide spread of team-games around their own team-season mean (leave-one-out, centered), added to the
team's per-script estimate. It describes past variation, not a probability. The baseline is the team's observed script
mix in the filter (the league's mix when the team has no classified game); the page shows the change in the central
estimate between the baseline and the scenario.

**Matchup (when an opponent is chosen).** The opponent's plays allowed per game (shrunk, k = 4 games) and the
additive assumption team + opponent allowed - league mean, labelled with the backtest verdict for that assumption.

<!-- PREREG-START -->
## 4. Preregistered backtest (frozen before the real-data run)

* **Spec**: `pace_backtest.SPEC`, version `pace-bt-0.1.0`.
* **Data**: nflverse 2019-2025 regular seasons, ingested by `scripts/pace_lab_backtest.py history` through
  `app/nfl/history.py`'s own writer (schedules + play-by-play aggregates only) into a separate file.
* **Targets**: (1) a team's plays in each game from week 2 on, overtime games excluded; (2) its neutral pass rate in
  each game with a neutral play, weighted by that game's neutral plays.
* **Point-in-time**: a prediction for (S, W) uses only counted games of season S with week < W; the team needs at
  least one earlier volume game that season (otherwise the row is skipped for every model). No previous-season data
  is used.
* **Baselines**: league mean to date; team's season-to-date raw mean (raw rate for the neutral pass rate).
* **Candidates**: (a) shrunk team mean, k = 4 games (150 neutral plays); (b) additive matchup: (a) + shrunk opponent
  plays allowed - league mean; (c) the lab's script-mixture central estimate with the team's observed script mix to
  date (league mix without a classified game). Neutral pass rate: (a) only.
* **Metric**: MAE (headline), RMSE, bias; per season. Paired MAE differences candidate - baseline with a 95% cluster
  bootstrap interval over (season, week) clusters, 2,000 draws, seed 20261023.
* **Verdict**: *improves* only if the candidate's MAE is below both baselines' and both intervals lie below 0; *worse*
  if its MAE is above the better baseline's and that interval lies above 0; otherwise *no clear difference*.
* **Ranges**: share of games inside the 10-90% range for the realized script (conditional) and for the pregame
  mixture; nominal 80%. Reported, not tuned.
* **Decision rule for the product**: whatever the verdict, nothing is promoted and no forecast path changes. The page
  states the verdict next to each assumption. A candidate that does not *improve* is labelled as not beating the
  simple baseline.
<!-- PREREG-END -->

## 5. Results (run once on the frozen spec)

Run 2026-10-23 on nflverse 2019-2025 (7 regular seasons, 3,742 offense-games, no game without a team-week row,
overtime stored for every game; nflreadpy 0.1.5, data fingerprint `c80488fc2ce79d2e`), spec hash
`2e20c7edede8e664`, preregistration committed in `317f42d` before the run. The result file is
`backend/app/analytics/pace_lab_backtest.json`; a second run on the same data gave an identical result (apart from
the run time). Skipped: 224 week-1 games (no history), 188 overtime games, 16 games whose team had no earlier volume
game that season.

**Plays per game (n = 3,314 team-games)**

| Model | MAE | RMSE | Bias |
|---|---|---|---|
| League mean to date (baseline) | **6.45** | **8.04** | -0.03 |
| Team season-to-date mean (baseline) | 6.91 | 8.67 | -0.03 |
| Shrunk team mean, k = 4 | 6.46 | 8.08 | -0.03 |
| Shrunk team + opponent plays allowed - league | 6.58 | 8.24 | -0.03 |
| Script mixture (the lab's central estimate) | 6.59 | 8.24 | -0.01 |

Paired MAE difference against the league mean (95% cluster bootstrap): shrunk team +0.01 [-0.06, +0.08] (**no clear
difference**), additive matchup +0.13 [+0.04, +0.22] (**worse**), script mixture +0.13 [+0.04, +0.22] (**worse**).
All three candidates beat the raw team mean clearly (-0.45, -0.32, -0.32; every interval below 0). By season the
shrunk team mean beat the league mean in 2019, 2022 and 2025 and lost in the other four.

**Neutral pass rate (n = 3,517 team-games, weighted by neutral plays)**

| Model | Weighted MAE |
|---|---|
| League rate to date | 0.0763 |
| Team season-to-date rate | 0.0759 |
| Shrunk team rate, k = 150 neutral plays | **0.0723** |

The shrunk rate **improves** on both baselines: -0.0040 [-0.0049, -0.0030] against the league rate and -0.0036
[-0.0053, -0.0022] against the raw team rate, and has the lowest MAE in every season.

**Ranges.** The 10-90% range for the script that happened contained 81.6% of games (2,545 of 3,120); the pregame
mixture range 82.8% (2,584 of 3,120); nominal 80%. Both are close to nominal and slightly wide.

**Reading.** Within a season, a team's past play volume does not forecast its next game's plays better than the
league average: per-game volume is mostly noise around a common level, and both the opponent adjustment and the
outcome-based script mix add error. The lab therefore shows team pace and script tendencies as **descriptions of
past games**, says on the page that they did not beat the league average as a forecast, and labels the scenario as
hypothetical. Its ranges describe the spread of past games and covered close to their nominal share in this
backtest. The shrunk neutral pass rate is the one estimate here that forecast better than the simple baselines (by
about 0.4 percentage points of MAE). Nothing is promoted; no projection or forecast path reads any of it.

Not tested: previous-season priors, other prior strengths, seconds-per-play pace (not stored), and play-level score
state (not stored). Any of these would be a new preregistered version.

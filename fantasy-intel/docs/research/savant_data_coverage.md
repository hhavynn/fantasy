# Savant-style metrics: data coverage and freshness audit

Date: 2026-10-18 (live season 2026, week 6 in progress). Research only. Nothing here changes ifp-2.0.0,
the weekly pipeline or any production projection.

Code and data:

* `scripts/savant_data_coverage.py`: the coverage audit (read-only, downloads from nflverse, writes no database)
* `docs/research/savant_data_coverage_weekly.csv`: one row per (source, metric, season, week), 2016-2026
* `docs/research/savant_data_coverage_season.csv`: the same rolled up per season
* `backend/app/nfl/sync.py`: new raw table `nfl_ngs_passing` (tests: `backend/tests/test_nfl_ngs_passing.py`)

**Why this exists.** NFL Savant's metrics are built on nflverse / nflfastR data, which we already ingest.
We decided not to add Savant as a live dependency. Instead we derive Savant-style metrics from our own
PBP, FTN and NGS sources and backtest them. Before anyone backtests, this audit answers two questions for
each input: does it exist for every season and week from 2016 to 2026, and is it available before the
next week's games (so it can be a weekly pregame input), or only later?

## 1. Verdict: which inputs are safe as live weekly inputs

"Usable pregame weekly" means week W's data is reliably published before week W+1 kicks off (Thursday
night), so it can feed a projection made during week W+1. "Research-only / delayed" means the data
arrives after the season, has no in-season update schedule, or contains future information.

| Metric / field | Source (our table) | Seasons | Classification | Evidence on freshness |
|---|---|---|---|---|
| `epa`, `success`, `wp` | PBP (`load_pbp`, derived tables) | 2016-2026, 0% null | **Usable pregame weekly** | Hours after games (see §3) |
| `down`, `ydstogo`, `yardline_100` | PBP | 2016-2026, 0% null | **Usable pregame weekly** | Same |
| `air_yards`, `yards_after_catch` | PBP | 2016-2026, 0% null on attempts / completions | **Usable pregame weekly** | Same |
| `cp`, `cpoe` | PBP (nflfastR model) | 2016-2026, 1.3-4.4% null on attempts | **Usable pregame weekly** (model output: see caveat 4) | Same |
| `xpass`, `pass_oe` | PBP (nflfastR model) | 2016-2026, 0% null | **Usable pregame weekly** (model output) | Same |
| `xyac_mean_yardage` | PBP (nflfastR model) | 2016-2026, 8-11% null on attempts | Usable pregame weekly, with care: ~10% of attempts have no xYAC | Same |
| `qb_hit`, `sack`, `qb_scramble` (pressure proxies) | PBP | 2016-2026, 0% null on dropbacks | **Usable pregame weekly** | Same |
| `penalty`, `penalty_type`, `penalty_yards`, `penalty_team` | PBP | 2016-2026, ≤0.2% null | **Usable pregame weekly** | Same |
| `penalty_player_id` | PBP | 2016-2026, 2.4-10.7% null on penalty plays | Usable, with care: team penalties (delay of game, too many men) have no player | Same |
| Expected fantasy points (`total_fantasy_points_exp` and parts) | ff_opportunity (`nfl_ff_opportunity`) | 2016-2026, 0% null, every played game | **Usable pregame weekly** | Updated after each game window (cron), hours after games |
| NGS passing: `aggressiveness`, `avg_time_to_throw`, `avg_intended_air_yards`, `completion_percentage_above_expectation`, ... | NGS (`nfl_ngs_passing`, **new**) | 2016-2026, ≤0.2% null | **Usable pregame weekly** for `week >= 1`. Week 0 is research-only (caveat 2) | Daily refresh 07:00 UTC in season; TNF week 6 was present within 2 days |
| NGS receiving: `avg_separation`, `avg_cushion`, `avg_yac_above_expectation`, ... | NGS (`nfl_ngs_receiving`) | 2016-2026, ≤1.0% null | **Usable pregame weekly** (`week >= 1`) | Same as NGS passing |
| NGS rushing: `efficiency`, `avg_time_to_los`, `percent_attempts_gte_eight_defenders` | NGS (`nfl_ngs_rushing`) | 2016-2026, 0% null | **Usable pregame weekly** (`week >= 1`) | Same as NGS passing |
| NGS rushing: `rush_yards_over_expected` (RYOE), `expected_rush_yards` | NGS (`nfl_ngs_rushing`) | **2018+** (100% null in 2016-2017) | Usable pregame weekly from 2018. Backtests before 2018 must treat it as missing | Same as NGS passing |
| FTN charting: `is_play_action`, `is_rpo`, `is_motion`, `is_screen_pass`, `n_defense_box`, `n_pass_rushers`, `n_blitzers`, `is_qb_out_of_pocket`, `is_catchable_ball`, `is_contested_ball`, `is_drop`, `is_interception_worthy`, `is_created_reception`, `read_thrown` | FTN (`load_ftn_charting`, read codes in `nfl_pbp_player_week`) | **2022+ only**, every played game, ≤0.03% null (one exception: `read_thrown` 2022, below) | **Usable pregame weekly, with lag**: FTN is hand-charted, about 1-2.5 days behind the game | Week 5 (including MNF) complete ≤ 27 h after MNF ended; TNF week 6 arrived 1-2 days later |
| PFR advanced stats (`times_pressured`, used as `pfr_pressures` in `nfl_team_week`) | PFR (`nfl_pfr_pass/rush/rec`) | already synced | Usable weekly, with a larger lag: week 6 TNF was still missing 2.5 days after the game | Week 5 present by Wednesday 05:57 UTC |
| Participation: `was_pressure`, `time_to_throw`, `defense_coverage_type`, `defense_man_zone_type`, `route`, `number_of_pass_rushers`, `defenders_in_box`, `offense_formation`/`personnel` | participation (`load_participation`, not synced) | 2016-2025. **No 2026 file**; coverage types null 2016-2017; `was_pressure` ~10% null and `route` ~62% null 2016-2022; near 0% null 2023+ | **Research-only / delayed** | No scheduled update job. The 2025 file was published 2026-02-10, after the season |
| NGS week 0 (season totals) in any NGS table | NGS | all | **Research-only** (leaks the rest of the season) | n/a |

The short version for the PR and the other workstreams:

* **Safe as live weekly inputs:** all PBP fields above (including nflfastR's cp/cpoe/xpass/xyac), ff_opportunity
  xFP, and NGS passing/receiving/rushing for `week >= 1`. RYOE only from 2018.
* **Safe with a lag caveat:** FTN charting (2022+ only). Check the week is complete before a Thursday projection
  run. PFR advanced stats lag further.
* **Research-only / delayed:** participation (true pressure `was_pressure`, `time_to_throw`, coverage, routes)
  and NGS week-0 season totals.

There is **no true pressure flag in PBP**. Weekly pressure features must use the PBP proxies (`qb_hit`,
`sack`, already combined as `hit_or_sack` in `nfl_team_week`), PFR `times_pressured` (lagged), FTN
`n_pass_rushers` / `n_blitzers` (2022+), or the NGS team-level `avg_time_to_throw`. Participation's
`was_pressure` is post-season only.

## 2. Coverage per season (2016-2026)

Null rates are measured only on the plays where the field is defined. A null `cpoe` on a run is not
missing data. Scopes, from `scripts/savant_data_coverage.py`:

* pass attempts (no sacks) for `cp`, `cpoe`, `air_yards`, `xyac`
* completions for `yards_after_catch`
* run/pass plays for `down`, `ydstogo`, `yardline_100`, `epa`, `success`, `wp` and `xpass`/`pass_oe`
* dropbacks for `qb_hit`, `sack` and `qb_scramble`
* penalty plays for the `penalty_*` detail
* every real play for the `penalty` flag

All are regular season only, with two-point tries excluded.

**Game coverage.** PBP, ff_opportunity and participation (2016-2025) cover every played regular-season
game in every week: 256 games a season in 2016-2020 and 272 in 2021+. 2022 has 271 because BUF-CIN
in week 17 was cancelled. FTN covers every played game in 2022-2025.

No historical week, in any game-level source, has fewer games than were played (checked per week in the
weekly CSV). Games missing in 2026 week 6 are not yet published, as §3 explains.

**NGS is not game-level.** It lists only players over NGS's weekly attempt threshold, about 30 QBs,
30 rushers and 73 receivers a week. In historical weeks it covers at least 25 teams for passing, 19
for rushing and 24 for receiving. A player missing from NGS that week is **missing, not zero**.

Fields with non-trivial null rates (everything else in the CSV is 0.0-0.2% null every season):

| Field | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (wk 1-6) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| pbp `cpoe` / `cp` | 1.3% | 1.5% | 2.4% | 3.6% | 3.5% | 3.2% | 3.9% | 4.2% | 4.1% | 4.3% | 4.4% |
| pbp `xyac_mean_yardage` | 8.3% | 8.1% | 9.2% | 9.8% | 10.4% | 9.5% | 9.9% | 10.2% | 10.8% | 10.7% | 10.8% |
| pbp `penalty_player_id` (penalty plays) | 3.3% | 2.4% | 6.6% | 6.7% | 7.3% | 7.8% | 8.6% | 8.2% | 8.5% | 8.3% | 10.7% |
| NGS rushing RYOE / `expected_rush_yards` | 100% | 100% | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0% | 0% |
| NGS receiving `avg_yac_above_expectation` | 0.4% | 0.6% | 1.0% | 0.5% | 0.4% | 0.2% | 0.2% | 0.5% | 0.2% | 0.2% | 0.0% |
| FTN (all charted fields) | n/a | n/a | n/a | n/a | n/a | n/a | 0-2.1% | 0-0.03% | 0% | 0% | 0% |
| participation `was_pressure` | 8.9% | 9.9% | 10.5% | 10.4% | 10.0% | 10.4% | 10.9% | 0.0% | 0.0% | 0.0% | no file |
| participation coverage / man-zone type | 100% | 100% | 11.0% | 10.7% | 10.3% | 10.5% | 11.0% | 0-0.4% | 0-0.2% | 0-0.1% | no file |
| participation `route` (per player) | 60.6% | 62.1% | 61.9% | 62.2% | 61.8% | 62.0% | 63.2% | 0.0% | 0.0% | 0.0% | no file |

Notes on the anomalies:

* **FTN `read_thrown` in 2022** is null on 2.1% of pass attempts. In 2023+ every play carries a code. The 2022
  encoding differs, so treat 2022 read codes as a separate population. The existing FTN read-code columns
  are already unverified (see `backend/app/nfl/ftn.py`).
* **FTN `n_pass_rushers` 2023 week 16** is null on about half of that week's dropbacks. It is the only gap of
  its kind.
* **Participation changed provider in 2023.** NGS-era participation (2016-2022) has gaps that FTN-era
  participation (2023+) does not. That break alone would shift any pressure or coverage feature trained across it.

## 3. Freshness: when a week becomes available

The files themselves cannot show when a week first appeared. nflverse overwrites release assets, and
FTN's `date_pulled` is reset on every re-pull: all of 2026 weeks 1-3 carry the same 2026-10-05 stamp. So
the evidence below comes from three places:

* nflverse's scheduled update jobs (GitHub Actions cron)
* our own `sync_runs`, which records `<table>_max_week` per run (2026-10-14 → 2026-10-18)
* release-asset timestamps and file contents at audit time (2026-10-18, about 21:00 UTC)

All times are UTC. Week 6 kicked off with TNF SEA@DEN on 2026-10-16 00:15 (ended about 03:30). Week 5's
MNF ended about 2026-10-13 03:30.

| Source | Update schedule (nflverse cron) | Observed |
|---|---|---|
| PBP (`nflverse-pbp`) | After each game window: Fri 05:33 (TNF), Sun 22:03 (early), Mon 00:07 (late), Mon/Tue 05:33 (SNF/MNF), plus daily 09:03 | Week 6 was in our 2026-10-16 04:50 sync, about 1.5 h after TNF ended. The 2026-10-18 21:03 release already held 9 of the 12 week-6 games played that day (TNF, London, the 1 pm window) |
| ff_opportunity (`ffverse/ffopportunity`) | 12-15 min after each PBP window | The release at 2026-10-16 10:07 contained week 6 TNF |
| NGS (`nflverse/ngs-data`) | Daily 07:00 in season | Week 6 TNF appeared in a sync between 2026-10-16 04:50 and 2026-10-18 00:30. The 2026-10-18 file (11:53) has only TNF for week 6, so Sunday games will land Monday at the earliest |
| FTN (`nflverse-ftn`) | Every 6 h, but FTN charts by hand | Week 5 was complete (79 games, including MNF) by the 2026-10-14 05:57 sync, ≤ 27 h after MNF. Week 6 TNF arrived 1-2 days after the game |
| PFR advanced (`nflverse-pfr`) | Every 6 h; PFR posts its weekly advanced stats later in the week | Week 5 was present 2026-10-14 05:57. Week 6 TNF was still absent 2026-10-18 |
| Snap counts (`nflverse-pfr`) | Every 6 h | Week 6 TNF was in the 2026-10-16 11:04 release |
| Participation (`nflverse-ftn` `update_participation`) | **No schedule** (manual `workflow_dispatch` only) | No 2026 file. The 2025 file was published 2026-02-10 |

Our live sync (`refresh_all`) runs several times a day, so for a projection made Wednesday to Saturday of week
W+1, every "usable pregame weekly" source above already holds all of week W. The tightest case is a
Thursday-night projection after an MNF game: PBP, ff_opportunity and NGS are in, and FTN usually is. A
feature that depends on FTN should check that week W is complete
(`nfl_pbp_player_week.ftn_charted_targets` is NULL for uncharted games, never 0) instead of assuming it.

**Measure, don't assume, going forward.** `sync_runs.stats` already records `nfl_ngs_passing_max_week`
and the other tables' max week for every run. After a few more weeks, the first-seen time of each week
per table gives a measured lag distribution. Four days of history are enough to classify the sources,
but not to put tight bounds on FTN or PFR lags.

## 4. Caveats for backtests

1. **Missing is not zero.** NGS lists qualifying players only, FTN starts in 2022, RYOE starts in 2018, and
   participation 2016-2022 has ~10% null pressure. Every one of these must enter a model as missing (with a
   missing indicator or a fallback), never 0. The sync preserves nulls; feature code must too.
2. **NGS week 0 = season totals.** Each NGS table carries a `week = 0` row per player with that season's
   totals. For past seasons that is the full season, so it leaks future weeks. Always filter `week >= 1`
   (as `analytics/usage.py` already does for `nfl_ngs_receiving`) and build to-date averages yourself.
3. **NGS max week includes the playoffs.** For example, 2016 runs to week 22. Filter `season_type = 'REG'` when
   matching regular-season weeks.
4. **Model outputs and history get revised.** `cp`, `cpoe`, `xpass`, `pass_oe` and `xyac_*` are nflfastR model
   outputs, recomputed whenever nflverse rebuilds a season: the 2025 PBP was rebuilt 2026-08-13 and FTN 2025
   was re-pulled 2026-09-22. A backtest therefore sees today's model values, not the values that were live
   that week. The difference is usually small, but it is a form of look-ahead the live model never gets. Raw
   counts (`air_yards`, `qb_hit`, `penalty`) are far less affected.
5. **FTN has no pre-2022 history**, so a feature that needs FTN cannot be trained on 2016-2021. Its backtest
   window is 2022-2025 plus live 2026.
6. **Provider break in participation (2022 → 2023)**: see §2.

## 5. NGS passing ingestion (`nfl_ngs_passing`)

Added to `RAW_DATASETS` in `backend/app/nfl/sync.py`, following `nfl_ngs_rushing` and `nfl_ngs_receiving`:
`nfl.load_nextgen_stats([season], stat_type="passing")`, stored as published in a season partition
(`replace_frame_partition`), with no zero-filling and no derived columns. A failed download becomes a sync
warning, like every other raw dataset, and does not stop the sync. The source loaded on every attempt in
this audit (2016-2026, one file covering all seasons).

Key columns (one row per QB-week; key `season, season_type, week, player_gsis_id`):

| Column | Meaning |
|---|---|
| `avg_time_to_throw` | seconds from snap to throw |
| `aggressiveness` | % of attempts into tight coverage (defender within 1 yard at the catch point) |
| `avg_intended_air_yards` / `avg_completed_air_yards` | depth of target on all attempts / completions |
| `avg_air_yards_differential` | completed minus intended air yards |
| `avg_air_yards_to_sticks` | intended air yards relative to the first-down line |
| `expected_completion_percentage` / `completion_percentage_above_expectation` | NGS's xComp% and CPOE (NGS's model, not nflfastR's `cpoe`) |
| `max_completed_air_distance`, `avg_air_distance`, `max_air_distance` | throw distances |
| `attempts`, `completions`, `pass_yards`, `pass_touchdowns`, `interceptions`, `passer_rating` | box score for the week |
| `player_gsis_id`, `team_abbr`, `player_display_name`, `player_position`, ... | identity (`player_gsis_id` matches `nfl_players.gsis_id`) |

**Not wired into ifp-2.** No feature, model or analytics module reads `nfl_ngs_passing`. The QB-receiver
deep-ball and situational-usage workstreams may consume it. They should follow caveats 1-3.

**History DB.** `app/nfl/history.py` deliberately syncs only the datasets the forecast features use. It
fetches no NGS, PFR, FTN or depth charts, so it was not extended. A backtest that needs NGS history can call
`nfl.load_nextgen_stats(seasons, stat_type=...)` directly: one file covers 2016+. Alternatively, a later change
can add all three NGS tables to the history sync together.

## 6. Reproduce

```
cd backend && uv run python ../scripts/savant_data_coverage.py --seasons 2016-2026
```

The script downloads about 11 seasons of PBP (cached by nflreadpy), so the first run takes a few minutes.
The 2026 rows reflect the files at run time. Re-running later in the season changes the 2026 rows and nothing else.

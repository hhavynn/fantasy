# Breakout Radar v1: usage growth versus noise (issue #212, research only)

**Status:** research only. Nothing in the app imports `app/analytics/breakout_radar.py`
(`tests/test_breakout_radar.py::test_research_module_is_never_imported_by_the_app`). No API, UI, table, forecast or
artifact changes. Whether an API and a roster-scoped page follow is the merge captain's decision, made from the
numbers below.

## Question

When a player's opportunity has risen over his last games, is it a lasting change in role or noise? A useful radar
has to beat the naive answer, "whoever just had a big game", on the same player-weeks.

## Data

- nflverse, through `nflreadpy` 0.1.5. The research file was synced on 2026-10-23 by `scripts/sync_nfl_history.py`
  (finished seasons only; never the live database).
- Regular seasons 2021–2025 (weeks 1–18), RB, WR and TE: 31,911 player-games of `player_week_usage`, built by the
  production builder (`app/analytics/usage.py`) inside the history file.
- **Opportunity metric: `xfp`**, nflverse's expected PPR points from the player's realized opportunity (targets,
  carries, air yards and field position), per game played.
- A missed game has no row, and it is never a zero. A null `xfp` is unknown, and the player is then `insufficient`.
- Route participation is not used. Routes come only from a licensed external provider (`route_data_provider`), and
  none is present: missing, not zero.

## The radar (fixed before any outcome was looked at)

For each player as of week W of a season, using only his games in weeks ≤ W of that season:

- `base(k)` is the mean `xfp` of up to 4 games before his most recent `k` games. At least 2 such games are required.
- `up(b) = b + max(3, 0.5·b)` and `down(b) = b − max(3, 0.33·b)`. The bar is absolute for small roles and relative for
  big ones, so 1 → 2 xFP is not a "doubling".
- `k` is the largest run of most-recent games that each clear `up(base(k))`, with `base(k)` taken before that run.

The states:

| State | Rule |
|---|---|
| **emerging** | `k = 2`: two straight games well above his earlier level |
| **sustained** | `k ≥ 3` |
| **fading** | not rising, and his last 2 games each at or below `down(base(2))` |
| **steady** | anything else, including a one-game spike (`k = 1`): one game is not a role change |
| **insufficient** | too few games; last game older than week W−1; a team change inside the window; or a missing `xfp` |

The thresholds were set once, in the module, before the evaluation ran, and nothing was tuned on outcomes. A later
change of a threshold, followed by re-reporting these numbers, would make them an in-sample fit.

## Evaluation

The evaluation is walk-forward (`breakout_radar.evaluate`).

- **Cuts.** For every week W from 4 to the last week minus H, every player with a game by W is cut using only
  weeks ≤ W. `test_no_look_ahead_later_games_never_change_a_state` checks that adding or changing later games never
  changes a state.
- **Label.** Each eligible player is labelled from his games in weeks W+1..W+H only, for H = 2 and H = 4:
  - **elevated** when their mean `xfp` is at or above `up(B)`, where `B = base(2)` is his level before his last two
    games (the same B for every player);
  - **fell** when it is at or below `down(B)`;
  - **unlabelled** (counted, never a negative) when he played fewer than half of the H weeks.
- **Two naive recent-volume baselines on the same labelled player-weeks:**
  - *one-game spike*: his last game is at or above `up(B)`;
  - *matched volume*: per (season, week, position), as many flags as the radar's emerging ones, given to the players
    with the largest last-game increase over B.
- **Uncertainty.** The precision gap against matched volume has a 95% interval from a paired bootstrap (2,000
  resamples, seed 212) that resamples whole (season, week) cuts, because players in one week are not independent.

## Results

Pooled over 2021–2025. N is labelled player-weeks; coverage is eligible ÷ player-weeks with a game in the window.

### Horizon 2 weeks

| Position | Labelled N | Coverage | Base rate | Emerging flags / hits | Emerging precision | Recall | FPR | Matched-volume precision | FPR | One-game spike: flags, precision | FPR | Gap vs matched (95% CI) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 21,173 | 69.4% | 14.7% | 803 / 358 | **44.6%** | 11.5% | 2.5% | 37.9% | 2.8% | 3,435, 34.2% | 12.5% | **+6.7 (+2.9, +10.5)** |
| RB | 6,229 | 67.7% | 15.3% | 236 / 111 | 47.0% | 11.6% | 2.4% | 44.9% | 2.5% | 1,011, 37.9% | 11.9% | +2.1 (−5.3, +9.6) |
| WR | 9,208 | 68.3% | 15.5% | 377 / 170 | 45.1% | 11.9% | 2.7% | 34.2% | 3.2% | 1,579, 33.8% | 13.4% | +10.9 (+5.5, +16.3) |
| TE | 5,736 | 73.5% | 13.0% | 190 / 77 | 40.5% | 10.4% | 2.3% | 36.3% | 2.4% | 845, 30.4% | 11.8% | +4.2 (−3.5, +12.8) |

### Horizon 4 weeks

| Position | Labelled N | Coverage | Base rate | Emerging flags / hits | Emerging precision | Recall | FPR | Matched-volume precision | FPR | One-game spike: flags, precision | FPR | Gap vs matched (95% CI) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 17,315 | 69.8% | 13.0% | 688 / 296 | **43.0%** | 13.2% | 2.6% | 36.9% | 2.9% | 2,845, 32.1% | 12.8% | **+6.1 (+2.6, +9.8)** |
| RB | 5,102 | 68.0% | 14.2% | 206 / 106 | 51.5% | 14.6% | 2.3% | 43.2% | 2.7% | 832, 37.0% | 12.0% | +8.3 (+1.4, +15.3) |
| WR | 7,512 | 68.8% | 13.5% | 322 / 126 | 39.1% | 12.5% | 3.0% | 32.3% | 3.4% | 1,319, 30.6% | 14.1% | +6.8 (+1.5, +12.8) |
| TE | 4,701 | 73.6% | 10.8% | 160 / 64 | 40.0% | 12.6% | 2.3% | 38.1% | 2.4% | 694, 29.0% | 11.8% | +1.9 (−6.5, +10.3) |

### By season (all positions)

| Season | Horizon | Labelled N | Base rate | Emerging flags | Emerging precision | Matched-volume precision | Gap (95% CI) |
|---|---|---|---|---|---|---|---|
| 2021 | 2 | 4,256 | 16.6% | 189 | 44.4% | 34.9% | +9.5 (+2.4, +16.5) |
| 2021 | 4 | 3,463 | 14.5% | 162 | 39.5% | 34.0% | +5.6 (−0.7, +12.2) |
| 2022 | 2 | 4,178 | 13.5% | 155 | 39.4% | 35.5% | +3.9 (−4.3, +12.0) |
| 2022 | 4 | 3,417 | 11.7% | 134 | 44.8% | 35.1% | +9.7 (+0.8, +17.3) |
| 2023 | 2 | 4,296 | 13.3% | 144 | 46.5% | 39.6% | +6.9 (−2.9, +18.6) |
| 2023 | 4 | 3,514 | 12.1% | 132 | 40.2% | 35.6% | +4.5 (−3.4, +14.6) |
| 2024 | 2 | 4,218 | 15.8% | 173 | 48.6% | 47.4% | +1.2 (−6.2, +6.8) |
| 2024 | 4 | 3,446 | 14.5% | 141 | 46.1% | 41.8% | +4.3 (−3.7, +12.1) |
| 2025 | 2 | 4,225 | 14.5% | 142 | 43.7% | 31.0% | +12.7 (+3.2, +22.7) |
| 2025 | 4 | 3,475 | 12.2% | 119 | 45.4% | 38.7% | +6.7 (−4.6, +18.0) |

### Other states (pooled, all positions)

- **Fading** (label "fell").
  - H = 2: 1,293 flags, precision 50.4% against a base rate of 14.2%, FPR 3.5%.
  - H = 4: 1,068 flags, precision 48.6% against a base rate of 11.6%, FPR 3.6%.
  - No naive comparator is reported for fading.
- **Sustained.**
  - Precision 34.9% at H = 2 (716 flags) and 33.3% at H = 4 (589 flags).
  - Its label is measured against B, which already holds part of its run, so this is a harder bar than emerging's.
    These numbers are reported, not compared.
- **Insufficient** (H = 2, of 33,114 player-weeks):
  - 6,656 had too few games (early weeks, part-time players, rookies);
  - 3,032 had no recent game (injury, IR, inactive);
  - 429 changed team (a trade).
  - A further 1,824 eligible player-weeks were unlabelled (he played less than half of the next two weeks).

## Reading

- **Emerging beats the naive baselines, pooled.** Requiring two straight games above his earlier level, rather than
  one big game:
  - raises precision by **+6.7 points (95% CI +2.9 to +10.5) at two weeks** and **+6.1 (+2.6 to +9.8) at four**,
    against the same number of "biggest last-game jump" flags;
  - cuts the false-positive rate from 12.5% to 2.5% against the one-game-spike rule, at a lower recall.
- **Positive in every season, but not decisive in each.** The gap's point estimate is positive in every season
  (+1.2 to +12.7). Single-season intervals often include zero, and 2024 at two weeks is close to even.
- **Strongest for WR**, which is positive at both horizons. RB is positive at four weeks only. TE's intervals include
  zero at both horizons.
- **Most emerging flags are still false alarms.** Precision is 43–45%, about 3× the 13–15% base rate but under one
  half. Recall is low (11–13%): most role growth is never flagged two games in a row before it shows. A page built on
  this must say so in words (for example "about 4 in 10 held for the next two weeks, 2021–2025"), not as odds for one
  player.

## Limitations

- **What the label measures.** It is persistence of *opportunity* (`xfp`), not fantasy points, and not a "breakout"
  in production. Efficiency, touchdowns and game script are not judged.
- **xFP is nflverse's model**, with its own coverage and definitions. Seasons before 2021 are not evaluated: they
  have 17 regular-season weeks, and the radar's postseason cut assumes 18.
- **No context features.** There are no injuries to teammates, depth charts, news or snap shares in the rule. Team
  changes are excluded, not modelled. Route participation is unavailable (licensed source, not present).
- **No league ownership or waiver eligibility.** This is v1 research: the evaluation covers every NFL RB, WR and TE
  with games, not one league's free agents.
- **One look at five seasons with fixed thresholds.** Re-tuning the thresholds against these results and re-reporting
  them would overstate the gap.
- **Position results are secondary comparisons**, read without a multiplicity correction.

## Reproduce

```
cd fantasy-intel/backend
uv run python ../scripts/sync_nfl_history.py --seasons 2021-2025 --db /path/nfl_history.duckdb
uv run python ../scripts/breakout_radar_research.py --history-db /path/nfl_history.duckdb --seasons 2021-2025 --out result.json
```

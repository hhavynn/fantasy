# Breakout Radar v1: usage growth versus noise (issue #212, research only)

**Status:** research only; **result: no above-baseline signal** (see Reading). Nothing in the app imports `app/analytics/breakout_radar.py`
(`tests/test_breakout_radar.py::test_research_module_is_never_imported_by_the_app`). No API, UI, table, forecast or
artifact changes. Whether an API and a roster-scoped page follow is the merge captain's decision, made from the
numbers below.

## Question

When a player's opportunity has risen over his last games, is it a lasting change in role or noise? A useful radar
has to beat naive recent-volume rankings on the same player-weeks, including one that reads the same two games it
does.

## Data

- nflverse, through `nflreadpy` 0.1.5. The research file was synced on 2026-10-23 by `scripts/sync_nfl_history.py`
  (finished seasons only; never the live database).
- Regular seasons 2021–2025 (weeks 1–18): 35,272 skill-position player-games of `player_week_usage`, of which 31,911 are
  RB, WR or TE. The rows were built by the production builder (`app/analytics/usage.py`) inside the history file.
  Every position is loaded, so a game listed at another position (FB, QB) is a game played, never a gap.
- **Opportunity metric: `xfp`**, nflverse's expected PPR points from the player's realized opportunity (targets,
  carries, air yards and field position), per game played.
- A missed game has no row, and it is never a zero. A null `xfp` is unknown, and the player is then `insufficient`.
- Route participation is not used. Routes come only from a licensed external provider (`route_data_provider`), and
  none is present: missing, not zero.

## The radar (fixed before any outcome was looked at)

For each player as of week W of a season, using only his games in weeks ≤ W of that season:

- `base(k)` is the mean `xfp` of up to 4 games before his most recent `k` games, inside a window of his last 6 games
  (so a 3-game run has a 3-game baseline and a 4-game run a 2-game one). At least 2 such games are required.
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
| **insufficient** | too few games; last game older than week W−1; a missing `xfp` or team; or a team or listed position (e.g. FB) that changes inside the window |

The thresholds were set once, in the module, before the evaluation ran, and nothing was tuned on outcomes. A later
change of a threshold, followed by re-reporting these numbers, would make them an in-sample fit.

## Evaluation

The evaluation is walk-forward (`breakout_radar.evaluate`).

- **Cuts.** For every week W from 4 to the season's last week minus H, every player is cut using only weeks ≤ W. That
  includes his position, taken from his latest game up to W. `test_no_look_ahead_later_games_never_change_a_state`
  and `test_position_is_taken_as_of_the_week_never_from_a_later_row` check this.
- **Player-weeks** (the coverage denominator) are every week from his first game of the season on, at a radar position
  as of that week. A week after he stopped playing (injury, IR, release, inactive) counts as `no_recent_game`.
- **Label.** Each eligible player is labelled from his games in weeks W+1..W+H only, for H = 2 and H = 4:
  - **elevated** when their mean `xfp` is at or above `up(B)`, where `B = base(2)` is his level before his last two
    games (the same B for every player);
  - **fell** when it is at or below `down(B)`;
  - **unlabelled** (counted per state, never a negative) when he played fewer than half of the H weeks. At H = 2 that
    is neither of them: one game of two is enough for a label.
- **Three naive recent-volume baselines on the same labelled player-weeks:**
  - **two-game matched (the fair comparator).** Per (season, week, position), as many flags as the radar's emerging
    ones, given to the players with the largest *mean of their last two games* minus B. It reads exactly the games the
    radar reads, so it isolates the radar's rule from the benefit of having seen two games.
  - *last-game matched*: the same, ranked by his last game minus B;
  - *one-game spike*: his last game is at or above `up(B)`. This rule flags about four times as many players, so its
    FPR is not comparable at equal volume.
- **Uncertainty.** Precision gaps have 95% paired bootstrap intervals (2,000 resamples, seed 212, interpolated
  percentiles) under two clusterings:
  - whole (season, week) cuts;
  - whole player-seasons. The same player recurs in adjacent weeks, and at H = 4 adjacent labels share games.

  Both are reported; read the wider one.

## Results

Pooled over 2021–2025. N is labelled player-weeks. Gaps are in percentage points, with 95% CIs by week / by
player-season.

### Horizon 2 weeks

| Position | Labelled N | Coverage | Base rate | Emerging flags / hits | Precision | Recall | FPR | Two-game matched: precision, FPR | **Gap vs two-game matched** | Last-game matched: precision | Gap vs last-game matched | One-game spike: flags, precision, FPR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 21,158 | 69.4% | 14.8% | 803 / 358 | 44.6% | 11.5% | 2.5% | 42.2%, 2.6% | **+2.4 (-0.5, +5.6) / (-1.0, +6.0)** | 37.9% | +6.7 (+2.9, +10.5) / (+2.6, +10.9) | 3,434, 34.2%, 12.5% |
| RB | 6,228 | 67.7% | 15.3% | 236 / 111 | 47.0% | 11.6% | 2.4% | 50.4%, 2.2% | **-3.4 (-9.3, +2.4) / (-10.8, +3.8)** | 44.9% | +2.1 (-5.3, +9.5) / (-5.3, +9.7) | 1,011, 37.9%, 11.9% |
| WR | 9,194 | 68.2% | 15.5% | 377 / 170 | 45.1% | 11.9% | 2.7% | 40.1%, 2.9% | **+5.0 (+0.3, +9.6) / (+0.0, +10.3)** | 34.2% | +10.9 (+5.5, +16.3) / (+5.2, +16.5) | 1,578, 33.8%, 13.4% |
| TE | 5,736 | 73.5% | 13.0% | 190 / 77 | 40.5% | 10.4% | 2.3% | 36.3%, 2.4% | **+4.2 (-2.7, +12.4) / (-2.9, +11.0)** | 36.3% | +4.2 (-3.5, +12.8) / (-4.1, +12.5) | 845, 30.4%, 11.8% |

### Horizon 4 weeks

| Position | Labelled N | Coverage | Base rate | Emerging flags / hits | Precision | Recall | FPR | Two-game matched: precision, FPR | **Gap vs two-game matched** | Last-game matched: precision | Gap vs last-game matched | One-game spike: flags, precision, FPR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 17,301 | 69.7% | 13.0% | 688 / 296 | 43.0% | 13.2% | 2.6% | 42.0%, 2.7% | **+1.0 (-2.2, +4.2) / (-2.6, +4.9)** | 36.9% | +6.1 (+2.6, +9.8) / (+1.9, +10.8) | 2,843, 32.1%, 12.8% |
| RB | 5,102 | 68.0% | 14.2% | 206 / 106 | 51.5% | 14.6% | 2.3% | 49.0%, 2.4% | **+2.4 (-3.8, +8.2) / (-5.5, +10.3)** | 43.2% | +8.3 (+1.4, +15.3) / (+0.3, +16.5) | 832, 37.0%, 12.0% |
| WR | 7,498 | 68.6% | 13.5% | 322 / 126 | 39.1% | 12.5% | 3.0% | 37.6%, 3.1% | **+1.6 (-2.5, +5.7) / (-3.9, +7.0)** | 32.3% | +6.8 (+1.5, +12.8) / (+0.6, +13.1) | 1,317, 30.6%, 14.1% |
| TE | 4,701 | 73.6% | 10.8% | 160 / 64 | 40.0% | 12.6% | 2.3% | 41.9%, 2.2% | **-1.9 (-9.6, +6.5) / (-8.8, +5.4)** | 38.1% | +1.9 (-6.5, +10.3) / (-6.0, +10.3) | 694, 29.0%, 11.8% |

### By season (all positions)

| Season | Horizon | Labelled N | Emerging flags | Emerging precision | Two-game matched | Gap vs two-game (by week CI / by player-season CI) | Last-game matched | Gap vs last-game |
|---|---|---|---|---|---|---|---|---|
| 2021 | 2 | 4,256 | 189 | 44.4% | 46.0% | -1.6 (-7.4, +6.0) / (-9.0, +5.7) | 34.9% | +9.5 (+2.4, +16.5) / (+1.7, +17.8) |
| 2021 | 4 | 3,463 | 162 | 39.5% | 44.4% | -4.9 (-10.6, +0.8) / (-12.5, +3.5) | 34.0% | +5.6 (-0.7, +12.1) / (-3.0, +15.1) |
| 2022 | 2 | 4,171 | 155 | 39.4% | 37.4% | +1.9 (-2.0, +5.9) / (-5.0, +9.6) | 35.5% | +3.9 (-4.3, +12.0) / (-4.7, +12.6) |
| 2022 | 4 | 3,410 | 134 | 44.8% | 38.1% | +6.7 (+0.8, +12.4) / (-1.8, +15.0) | 35.1% | +9.7 (+0.8, +17.1) / (+0.6, +19.6) |
| 2023 | 2 | 4,289 | 144 | 46.5% | 41.7% | +4.9 (-2.2, +12.6) / (-3.7, +13.4) | 39.6% | +6.9 (-2.9, +18.5) / (-1.9, +16.1) |
| 2023 | 4 | 3,507 | 132 | 40.2% | 38.6% | +1.5 (-4.2, +8.1) / (-7.4, +10.5) | 35.6% | +4.5 (-3.4, +14.6) / (-5.7, +15.4) |
| 2024 | 2 | 4,218 | 173 | 48.6% | 46.8% | +1.7 (-4.3, +7.0) / (-6.4, +9.5) | 47.4% | +1.2 (-6.2, +6.8) / (-7.8, +10.4) |
| 2024 | 4 | 3,446 | 141 | 46.1% | 46.1% | +0.0 (-7.6, +5.3) / (-8.3, +9.1) | 41.8% | +4.3 (-3.7, +12.1) / (-4.7, +14.4) |
| 2025 | 2 | 4,224 | 142 | 43.7% | 37.3% | +6.3 (-2.7, +15.2) / (-2.2, +15.1) | 31.0% | +12.7 (+3.2, +22.7) / (+2.9, +22.2) |
| 2025 | 4 | 3,475 | 119 | 45.4% | 42.0% | +3.4 (-6.7, +14.2) / (-6.3, +13.7) | 38.7% | +6.7 (-4.6, +18.0) / (-3.5, +17.1) |
### Other states (pooled, all positions)

- **Fading** (label "fell").
  - H = 2: 1,293 flags, precision 50.4% against a base rate of 14.2%, FPR 3.5%.
  - H = 4: 1,068 flags, precision 48.6% against a base rate of 11.6%.
  - No naive comparator is reported for fading.
- **Sustained.** Precision 34.9% (716 flags) at H = 2 and 33.3% (589) at H = 4. Its label is a harder bar, because B
  already holds part of its run, so these numbers are reported, not compared.
- **Unlabelled** (H = 2): 1,820 eligible player-weeks, of which 45 were emerging. So 45 of 848 emerging flags (5%)
  had no game in either of the next two weeks, and are outside the precision.
- **Insufficient** (H = 2, of 33,114 player-weeks):
  - 6,656 had too few games (early weeks, part-time players, rookies);
  - 3,032 had no recent game;
  - 429 changed team;
  - 19 changed listed position inside the window.

## Reading

- **Two games beat one.** Requiring the last two games to be raised, rather than the last one:
  - lifts precision by about 6–7 points over the same number of "biggest last-game jump" flags (+6.7 at H = 2: CI
    +2.9 to +10.5 by week, +2.6 to +10.9 by player-season);
  - flags a quarter as many players as the one-game-spike rule, at a far lower FPR (2.5% against 12.5%).
- **The radar's rule does not beat simply ranking by the two-game increase.** Against the two-game matched baseline, the
  gap is **+2.4 points at H = 2 (CI −0.5 to +5.6 by week, −1.0 to +6.0 by player-season)** and **+1.0 at H = 4 (−2.2 to
  +4.2 / −2.6 to +4.9)**.
  - Both intervals include zero.
  - By position, every interval includes zero except WR at H = 2 (+5.0; +0.3 to +9.6 by week, +0.0 to +10.3 by
    player-season), which is a secondary comparison without a multiplicity correction.
  - By season, only 2022 at H = 4 by week excludes zero.
  - So v1 shows no evidence that the emerging state adds signal beyond "his last two games were well above his
    earlier level".
- **Most emerging flags are false alarms.** About 4 in 10 held for the next two weeks, among players who played at
  least one of them (5% played neither and are unlabelled). Recall is low (11–13%).
- **Recommendation.** Under #212's stopping rule, this is not an above-baseline signal. It should not be shipped as a
  "breakout" predictor. At most, it could become an evidence-only view: the player's recent and earlier xFP, side by
  side, with no "breakout" claim and with the hit rate above stated in words. The merge captain decides.

## Limitations

- **What the label measures.** It is persistence of *opportunity* (`xfp`), not fantasy points, and not a "breakout"
  in production. Efficiency, touchdowns and game script are not judged.
- **xFP is nflverse's model**, with its own coverage and definitions. Seasons before 2021 are not evaluated: they
  have 17 regular-season weeks, and the radar's postseason cut assumes 18.
- **No context features.** There are no injuries to teammates, depth charts, news or snap shares in the rule. Team
  changes are excluded, not modelled. Route participation is unavailable (licensed source, not present).
- **No league ownership or waiver eligibility.** This is v1 research: the evaluation covers every NFL RB, WR and TE
  with games, not one league's free agents.
- **Fixed thresholds, with the comparator added after review.** The thresholds were not tuned. The two-game comparator
  and the player-season clustering were added in review, after the first results were seen; they make the test
  harder, not easier. Re-tuning the thresholds against these results and re-reporting would overstate any gap.
- **Position results are secondary comparisons**, read without a multiplicity correction.

## Reproduce

```
cd fantasy-intel/backend
uv run python ../scripts/sync_nfl_history.py --seasons 2021-2025 --db /path/nfl_history.duckdb
uv run python ../scripts/breakout_radar_research.py --history-db /path/nfl_history.duckdb --seasons 2021-2025 --out result.json
```

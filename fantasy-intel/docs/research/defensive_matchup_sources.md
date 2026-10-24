# Defensive matchups: source matrix (issue #198, step 1)

Date: 2026-10-24 (live season 2026, through week 7). Research only. Nothing here changes an API, a page, a model or a
projection.

Code and data:

* `scripts/defensive_matchup_sources.py`: the measurement. It is read-only, downloads from nflverse through nflreadpy
  0.1.5 and writes no database.
* `docs/research/defensive_matchup_sources.csv`: one row per (source, metric, season), 2022-2026.

Reproduce: `cd backend && uv run python ../scripts/defensive_matchup_sources.py --seasons 2022-2026`.

## 1. Verdict

* **No free source assigns a defender to a receiver.** None of these sources has a field naming the defender who
  covered a receiver, a shadow assignment or a pre-snap alignment per player:
  * play-by-play (372 columns);
  * participation;
  * FTN charting;
  * NGS receiving.

  The script checks every column name against assignment-like terms (`assign`, `shadow`, `covering`,
  `coverage_player`, `alignment`, `matchup_defender`) and finds none. **A CB shadow or coverage assignment is
  therefore unsupported.** The page must say so. It must not show a "likely CB", even as a possibility, unless a
  trustworthy confirmed source is added later.
* **What does exist is team-level defensive tendency, plus a defender "involved" on a minority of plays:**

  | Question | Supported by | Kind | Live in-season? |
  |---|---|---|---|
  | How often does this defense play man vs zone, and which shells? | participation `defense_man_zone_type`, `defense_coverage_type` | direct observation, play-level, team-level | **No.** The file appears after the season (2026 has none) |
  | How often does it blitz, and how many rush? | FTN `n_blitzers`, `n_pass_rushers`; participation `number_of_pass_rushers` | direct observation (charted) | FTN yes, 1-2 days behind; participation no |
  | How heavy are its boxes against the run? | FTN `n_defense_box`; participation `defenders_in_box` | direct observation | FTN yes; participation no |
  | Where do opponents throw against it (left/middle/right, short/deep)? | PBP `pass_location`, `pass_length` | direct observation of the throw, not the coverage | **Yes**, hours after games |
  | How many CBs does it use (nickel / dime)? | participation `defense_positions` (2023+) | direct observation of personnel, not alignment | No |
  | How much space does a receiver get? | NGS `avg_cushion`, `avg_separation` | receiver-level weekly average, no defender | Yes, NGS threshold only |
  | Which defender broke up or intercepted the pass, or made the tackle? | PBP `pass_defense_1_player_id`, `interception_player_id`, tackler ids | **outcome credit**, not coverage | Yes |

* **Outcome credit is not coverage.** A credited pass defender appears on only 30-33% of incompletions and on no
  completion. A tackler is the player who finished the play, often a safety or linebacker after the catch. Linking a
  receiver to "the CB who defended him" through these fields would select on outcomes. Use them only as labelled
  outcome counts, never as an assignment.

## 2. Measured coverage (regular season, two-point tries excluded)

The missing rate is measured on the plays where the field is defined. A blank string counts as missing.

| Source | Field | Scope | 2022 | 2023 | 2024 | 2025 | 2026 (wk 1-7) |
|---|---|---|---|---|---|---|---|
| PBP | games | | 271 | 272 | 272 | 272 | 94 |
| PBP | `receiver_player_id` | pass attempts | 3.9% | 4.2% | 4.1% | 4.3% | 4.3% |
| PBP | `pass_location`, `pass_length` | pass attempts | 0.01% | 0.01% | 0.0% | 0.0% | 0.0% |
| PBP | `pass_defense_1_player_id` | incompletions | **70.0%** | 69.0% | 68.3% | 67.5% | 67.0% |
| PBP | `interception_player_id` | interceptions | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| PBP | a tackler id (solo, assist or with assist) | completions | 9.1% | 9.5% | 9.6% | 9.7% | 9.7% |
| participation | games | | 271 | 272 | 272 | 272 | **no file** |
| participation | `route` (targeted receiver only) | pass attempts | 4.0% | 3.2% | 3.2% | 2.7% | no file |
| participation | `defense_coverage_type`, `defense_man_zone_type` | pass attempts | 0.2% | 0.3% | 0.1% | 0.1% | no file |
| participation | `defense_players` (ids on field) | pass attempts | 0.0% | 0.0% | 0.0% | 0.0% | no file |
| participation | `defense_positions` | pass attempts | **column absent** | 0.0% | 0.0% | 0.0% | no file |
| participation | `number_of_pass_rushers` | dropbacks | 0.1% | 0.03% | 0.0% | 0.0% | no file |
| participation | `was_pressure` | dropbacks | 10.9% (sacks uncoded) | 0.0% | 0.0% | 0.0% | no file |
| participation | `defenders_in_box`, `defense_personnel` | scrimmage plays | ≤0.1% | 0.0% | 0.0% | 0.0% | no file |
| FTN | games | | 271 | 272 | 272 | 272 | 93 |
| FTN | `n_defense_box`, `starting_hash` | scrimmage plays | ≤0.4% | 0.0% | 0.0% | 0.0% | 1.0% |
| FTN | `n_blitzers`, `n_pass_rushers` | dropbacks | 0.0% | ≤0.03% | 0.0% | 0.0% | 1.0% |
| FTN | `is_contested_ball` | pass attempts | 0.0% | 0.0% | 0.0% | 0.0% | 1.0% |
| NGS receiving | player-weeks | week ≥ 1 | 1,273 (about 71 a week) | 1,295 | 1,253 | 1,213 | 444 (about 63 a week) |
| NGS receiving | `avg_cushion`, `avg_separation` | player-weeks listed | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |

Notes:

* **The 2026 FTN gap is the publication lag.** It is about 1% and one game short of PBP (93 vs 94 games): the most
  recent game was not yet charted. It is not a coverage hole. `docs/research/savant_data_coverage.md` §3 measures the
  lag at 1-2.5 days.
* **NGS lists only players above its weekly threshold.** A receiver not listed that week is **missing, not zero**.
* **CB personnel on pass attempts** (participation `defense_positions`):

  | Season | Mean CBs on the field | Snaps with 3+ CBs |
  |---|---|---|
  | 2023 | 2.75 | 70.6% |
  | 2024 | 2.71 | 65.9% |
  | 2025 | 2.63 | 59.7% |

  This says how many CBs were on the field, not where they lined up or whom they covered.
* **The participation provider changed in 2023**, and so did its encoding: blank strings instead of nulls, a new route
  vocabulary, and sacks coded from 2023 on. Any cross-era feature needs era handling, as the savant audit (§2) already
  records. 2022 has no `defense_positions` column.

## 3. Licensing and use

License positions are taken from what this repo already records. Nothing here is a new legal reading.

| Source | Recorded license | Use for a page | Evidence |
|---|---|---|---|
| PBP (nflfastR via nflverse) | CC-BY 4.0, with attribution | yes, with an attribution line | `docs/youtube_studio_plan.md` ("most nflverse data is CC-BY 4.0") |
| FTN charting | **CC-BY-SA 4.0**: the owner's licensing decision is still pending | **blocked until the owner decides**: share-alike may bind derived outputs | `docs/youtube_studio_plan.md`; `docs/independent_projection_engine.md` (ifp-fr-p1 waits on the same decision) |
| Participation 2023+ | FTN-era file (the provider changed in 2023). **Per-file license not verified here** | treat as FTN (pending) until the nflreadr data docs are checked | `savant_data_coverage.md` §2 |
| Participation 2016-2022 | NGS-era file. **Per-file license not verified here** | research only until verified | |
| NGS receiving | **Not recorded in this repo** | research only until verified | |

The page reads no paid source, no scraped source and no provider login (`docs/external_utilization_contract.md` §9).

## 4. Gaps the page must show as unavailable

* **Coverage assignment and CB shadow:** unsupported in every free source.
* **Current-season man/zone rates, coverage shells and routes:** unavailable in-season. The participation file comes
  after the season, so these could only be shown from last season, labelled with the season.
* **Routes run and route participation:** the participation `route` field is the *targeted* receiver's route only,
  not every receiver's. Routes run per receiver are not in any free source. This matches the existing decision in
  `docs/youtube_studio_plan.md`: no "routes" claims.
* **FTN-based tendencies:** they are in-season, but blocked on the CC-BY-SA decision.
* **Defender identity on a target:** outcome credit only (about 31% of incompletions, plus interceptions and
  tacklers). It is never an assignment.

## 5. What step 2 could build, subject to Session 1's go

These are read-only research indicators. Each one carries its numerator, denominator, window and source:

1. **Opponent pass direction profile (in-season, PBP, CC-BY).**
   * Per defense: the share of opponents' pass attempts left/middle/right and short/deep, with EPA and completion
     rate per cell.
   * Shown beside league average and the receiver's own direction profile (his targets by location).
   * Sample sizes are shown, and small cells are suppressed.
   * It is a tendency, not a coverage claim.
2. **Last season's man/zone and shell mix (participation, labelled "2025 season").** The page must state it is not
   this season. It is held for the license check of the participation file.
3. **Box count vs run efficiency and blitz rate (FTN).** Held until the CC-BY-SA decision.

Model use: none. A matchup indicator enters a projection only after a separately gated, leakage-free backtest shows it
improves holdout error, as the issue requires.

## Limitations

* License positions are what the repo records. The per-file licenses of participation and NGS were **not verified**
  against the nflreadr data docs in this pass.
* Coverage is measured from 2022 on. Earlier seasons are in `savant_data_coverage.md`.
* nflreadpy 0.1.5 refuses participation seasons after 2025, so "no file" for 2026 is how the library behaves today. A
  later release could change it.

# Defensive matchups: source matrix (issue #198, step 1)

Date: 2026-10-24 (live season 2026, through week 7). Research only. Nothing here changes an API, a page, a model or a
projection.

Code and data:

* `scripts/defensive_matchup_sources.py`: the measurement. It is read-only, downloads from nflverse via nflreadpy
  0.1.5 and writes no database.
* `docs/research/defensive_matchup_sources.csv`: one row per (source, metric, season), 2022-2026. Every row names its
  scope (the denominator). Seasons a source does not publish are rows whose `note` is the library's own message.

To reproduce: `cd backend && uv run python ../scripts/defensive_matchup_sources.py --seasons 2022-2026`.

## 1. Verdict

* **No free source pairs a defender with a receiver on a play.** The script checked five sources:
  * play-by-play (372 columns);
  * participation (20-26 columns);
  * FTN charting (29);
  * NGS receiving (23);
  * PFR advanced defense (29).

  Every season's column names were checked for pairing-like fragments (`assign`, `shadow`, `covering`,
  `coverage_player`, `alignment`, `matchup_defender`, `defender_id`, `covered_by`, `receiver_defender`), and none was
  found (CSV rows `assignment-like columns`). That a name is absent does not prove the data is absent, so the reasons
  this holds are given below:
  * **Participation** lists the defenders on the field (`defense_players`, `defense_positions`), but every
    `defense_positions` list is in **sorted order** (100% of pass attempts, 2023-2025, e.g.
    `CB;CB;CB;DE;DE;DT;DT;FS;ILB;ILB;SS`). The order encodes no alignment and no pairing. `route` describes only the
    targeted receiver.
  * **NGS receiving** is per receiver per week (cushion, separation). It has no defender field.
  * **PFR advanced defense** is per defender per week: targets, completions, yards and touchdowns allowed, passer
    rating allowed and aDOT. It has no receiver field and no play-level rows.

  **A CB shadow or a coverage assignment is therefore unsupported.** That includes a "likely CB" built by combining
  PFR targets-allowed with a receiver's targets: that would be an inference the acceptance criteria rule out. The
  page must say this is unavailable.
* **What exists:**

  | Question | Supported by | Kind | Live in-season? |
  |---|---|---|---|
  | How often does this defense play man vs zone, and which shells? | participation `defense_man_zone_type`, `defense_coverage_type` | direct observation, play-level, team-level | **No**: the file is published after the season (none for 2026) |
  | How often does it blitz, and how many rush? | FTN `n_blitzers`, `n_pass_rushers`; participation `number_of_pass_rushers` | direct observation (charted) | FTN: yes, 1-2 days behind. Participation: no |
  | How heavy are its boxes against the run? | FTN `n_defense_box`; participation `defenders_in_box` | direct observation | FTN: yes. Participation: no |
  | Where do opponents throw against it (left/middle/right, short/deep)? | PBP `pass_location`, `pass_length` | direct observation of the throw (from the offense's side), not of the coverage | **Yes**, hours after games |
  | How many CBs does it use (nickel, dime)? | participation `defense_positions` (2023+) | roster-position labels of the players on the field, not on-play roles or alignment | No |
  | How much space does a receiver get? | NGS `avg_cushion`, `avg_separation` | receiver-level weekly average, no defender | Yes, receivers above the NGS threshold only |
  | How often was a given defender targeted, and what did he allow? | PFR advanced defense `def_targets`, `def_*_allowed`, `def_adot` | **charted per-defender weekly totals**, not paired with any receiver | Yes, published later in the week |
  | Which defender broke up or intercepted the pass, or made the tackle? | PBP `pass_defense_1_player_id`, `interception_player_id`, tackler ids | **outcome credit**, not coverage | Yes |

* **Outcome credit is not coverage.**
  * A credited pass defender appears on 30-33% of incompletions, on every interception, and on **no completion**
    (CSV row `pass_defense_1_player_id on completions`: 100% missing in every season).
  * A tackler is the player who ended the play, often a safety or linebacker after the catch.
  * Linking a receiver to "the CB who defended him" through these fields selects on the outcome. Use them only as
    labelled outcome counts.

## 2. Measured coverage (regular season, two-point tries excluded)

The missing rate is measured on the plays (or rows) where the field is defined. A blank string counts as missing.

| Source | Field | Scope | 2022 | 2023 | 2024 | 2025 | 2026 (wk 1-7) |
|---|---|---|---|---|---|---|---|
| PBP | games | | 271 | 272 | 272 | 272 | 94 |
| PBP | `receiver_player_id` | pass attempts | 3.9% | 4.2% | 4.1% | 4.3% | 4.3% |
| PBP | `pass_location`, `pass_length` | pass attempts | 0.01% | 0.01% | 0.0% | 0.0% | 0.0% |
| PBP | `pass_defense_1_player_id` | incompletions (no INT) | **70.0%** | 69.0% | 68.3% | 67.5% | 67.0% |
| PBP | `pass_defense_1_player_id` | completions | **100%** | 100% | 100% | 100% | 100% |
| PBP | `pass_defense_1_player_id`, `interception_player_id` | interceptions | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| PBP | a tackler id (solo, assist or with assist) | completions | 9.1% | 9.5% | 9.6% | 9.7% | 9.7% |
| participation | games | | 271 | 272 | 272 | 272 | **no file** |
| participation | `route` (targeted receiver only) | pass attempts | 4.0% | 3.2% | 3.2% | 2.7% | no file |
| participation | `defense_coverage_type`, `defense_man_zone_type` | pass attempts | 0.2% | 0.3% | 0.1% | 0.1% | no file |
| participation | `defense_players` (ids on the field) | pass attempts | 0.0% | 0.0% | 0.0% | 0.0% | no file |
| participation | `defense_positions` | pass attempts | **column absent** | 0.0% | 0.0% | 0.0% | no file |
| participation | `number_of_pass_rushers` | dropbacks | 0.1% | 0.03% | 0.0% | 0.0% | no file |
| participation | `was_pressure` | dropbacks | 10.9% (sacks and scrambles uncoded) | 0.0% | 0.0% | 0.0% | no file |
| participation | `defenders_in_box`, `defense_personnel` | scrimmage plays | ≤0.1% | 0.0% | 0.0% | 0.0% | no file |
| FTN | games | | 271 | 272 | 272 | 272 | 93 |
| FTN | `n_defense_box`, `starting_hash` | scrimmage plays | ≤0.4% | 0.0% | 0.0% | 0.0% | 1.0% |
| FTN | `n_blitzers`, `n_pass_rushers` | dropbacks | 0.0% | ≤0.03% | 0.0% | 0.0% | 1.0% |
| FTN | `is_contested_ball` | pass attempts | 0.0% | 0.0% | 0.0% | 0.0% | 1.0% |
| NGS receiving | player-weeks | week ≥ 1 | 1,273 (about 71 a week) | 1,295 | 1,253 | 1,213 | 444 (about 63 a week) |
| NGS receiving | `avg_cushion`, `avg_separation` | listed player-weeks | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| PFR defense | defender-weeks | | 7,517 (937 defenders) | 7,573 | 7,632 | 7,557 | 2,668 (wk 1-6) |
| PFR defense | `def_targets` | defender-weeks | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| PFR defense | `def_completions_allowed`, `def_passer_rating_allowed` | defender-weeks with a target | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| PFR defense | `def_yards_allowed`, `def_receiving_td_allowed` | defender-weeks with a target | **10.6%** | 9.9% | 10.1% | 10.5% | 10.4% |
| PFR defense | `def_adot` | defender-weeks with a target | 0.1% | 0.04% | 0.1% | 0.1% | 0.0% |

Notes:

* **The 2026 FTN gap is the publication lag:** about 1%, and one game behind PBP (93 vs 94 games). It is not a
  coverage hole. `docs/research/savant_data_coverage.md` §3 measures the lag at 1-2.5 days. PFR runs a week further
  behind (weeks 1-6 at the time of the run).
* **PFR `def_yards_allowed` and `def_receiving_td_allowed` are missing on about a tenth of targeted defender-weeks.**
  That is missing, not zero: a total built from them must count those weeks as unknown.
* **NGS lists only players above its weekly threshold.** A receiver not listed that week is **missing, not zero**.
* **CB labels on pass attempts** come from participation `defense_positions`:

  | Season | Mean CB labels on the field | Pass attempts with 3+ CB labels |
  |---|---|---|
  | 2023 | 2.75 | 70.6% |
  | 2024 | 2.71 | 65.9% |
  | 2025 | 2.63 | 59.7% |

  These are roster-position labels, not on-play roles. 2024 also has 232 generic `DB` labels on pass attempts, which
  are not counted as CBs. The labels say how many CBs were on the field, not where they lined up or whom they covered.
* **The participation provider changed in 2023**, and so did the encoding:
  * blank strings instead of nulls;
  * a new route vocabulary;
  * sacks and scrambles coded from 2023 on;
  * no `defense_positions` column in 2022.

  Any cross-era feature needs era handling, as the savant audit (§2) already records.

## 3. Licensing and use

This section records only what the repo already says. Nothing here is a new legal reading.

| Source | What the repo records | Use for a page | Evidence |
|---|---|---|---|
| PBP (nflfastR via nflverse) | "most nflverse data is CC-BY 4.0". The per-file license is **not verified** | likely, with an attribution line, once the per-file license is checked | `docs/youtube_studio_plan.md` |
| FTN charting | CC-BY-SA 4.0. Publishing FTN-derived numbers needs attribution and a share-alike review, which the owner has **not decided** | **blocked until the owner decides** | `docs/youtube_studio_plan.md`; `docs/research/signal_shadows.md` (ShareAlike on shared adapted material); `docs/research/first_read_depth_chart_signals.md` (published derived numbers need a share-alike review); `docs/independent_projection_engine.md` (ifp-fr-p1 waits on the same decision) |
| Participation 2023+ | FTN-era file (the provider changed in 2023). Per-file license **not verified** | treat as FTN (pending) until the nflreadr data docs are checked | `savant_data_coverage.md` §2 |
| Participation 2016-2022 | NGS-era file. Per-file license **not verified** | research only until verified | |
| NGS receiving | **not recorded** | research only until verified | |
| PFR advanced defense | **not recorded**. The repo already flags PFR-sourced files as needing a per-file license check | research only until verified | `docs/youtube_studio_plan.md` |

The page reads no paid source, no scraped source and no provider login (`docs/external_utilization_contract.md` §9).

## 4. Gaps the page must show as unavailable

* **Coverage assignment and CB shadow:** unsupported in every free source. This also rules out a "likely CB" derived
  from PFR targets-allowed.
* **Current-season man/zone rates, shells and routes:** unavailable in-season, because the participation file comes
  after the season. They could only appear as last season's, labelled with that season.
* **Routes run and route participation:** participation `route` is the *targeted* receiver's route only. Routes run per
  receiver are not in any free source, which matches the existing decision in `docs/youtube_studio_plan.md` to make no
  "routes" claims.
* **FTN-based tendencies:** available in-season, but blocked on the CC-BY-SA decision.
* **Defender identity on a target:** only outcome credit (about 31% of incompletions and every interception, never a
  completion) and PFR's per-defender weekly totals. Neither is an assignment, and neither may be presented as one.

## 5. What step 2 could build, if Session 1 approves

Read-only research indicators. Each one carries its numerator, denominator, window and source.

1. **Opponent pass-direction profile** (in-season, PBP).
   * Per defense: the share of opponents' pass attempts going left, middle or right and short or deep, with
     completion rate and EPA per cell.
   * Guards:
     * A cell is shown only with **at least 30 attempts**. Below that it reads "too few attempts", never a rate.
     * EPA is shown with its n and no ranking, since it is noisy at these sizes.
     * Every profile is labelled **unadjusted for opponents** until an opponent-strength adjustment exists, and its
       test fixtures include a schedule-skew case.
     * It is never paired with a named defender. `pass_location` is the throw direction from the offense's side,
       and CBs flip or travel, so "the CB on that side" must not be inferred.
     * The receiver's own direction profile is shown only as his target mix, with no defender beside it.
   * Reuse: the existing opponent target mix in `backend/app/analytics/roster_context.py` (opponent RB/WR/TE target
     shares).
   * Release: gated on the PBP per-file license check (§3).
2. **Last season's man/zone and shell mix** (participation, labelled "2025 season"). The page must state that it is
   not this season. Held for the participation license check.
3. **Box count vs run efficiency, and blitz rate** (FTN). Held until the CC-BY-SA decision.

Model use: none. A matchup indicator enters a projection only after a separately gated, leakage-free backtest shows
it improves holdout error, as the issue requires.

## Limitations

* License positions are what the repo records. No per-file license was verified against the nflreadr data docs in
  this pass.
* Coverage is measured from 2022 on. Earlier seasons are in `savant_data_coverage.md`.
* "No file" for 2026 participation is what nflreadpy 0.1.5 reports ("Season must be between 2016 and 2025"). The
  script records that message rather than a fixed cutoff, so a later release that publishes 2026 shows up on the next
  run.
* The pairing check is by column name, backed by the structural checks in §1 (sorted position lists, receiver-only and
  defender-only grains). A source added later needs the same structural check.

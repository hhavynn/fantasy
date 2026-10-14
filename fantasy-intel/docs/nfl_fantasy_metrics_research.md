# NFL Data Availability & Feasibility Research for In-Season Fantasy Analytics

> **Status: background research, not the spec.** The authoritative, verified
> definitions are in [`nfl_metrics_verified_contract.md`](nfl_metrics_verified_contract.md).
> Several claims in the original version of this document were wrong and have been
> corrected in place below (marked **Corrected**): target-share denominator,
> rushing share (team vs backfield), the `qb_hit + sack` pressure formula, the
> goal-line TD-rate claim, the spread sign for implied totals, NGS missing-data
> handling. The FTN `read_thrown` numeric codes are **unresolved** (dictionary vs
> observed data conflict; nflverse/nflreadr#325): the app treats them as experimental.

**Audience**: Implementation / Backend Engineers  
**Target Environment**: Python (`nflreadpy`, `duckdb`, `polars`) + `nflverse` open-source ecosystem  
**Scope**: In-season current NFL season tracking beyond Points Per Game (PPG)  

---

## Executive Summary & Core Constraints

1. **Participation / Route Tracking Reality**:
   * True play-by-play participation tracking (`load_participation()`), which contains routes run, receiver alignment (slot vs. outside), and offensive/defensive personnel groupings (e.g., 11 personnel, Nickel), **is NOT available in real-time during the current season**.
   * Under an agreement with data provider FTN, `nflverse` only publishes participation files **after the post-season concludes** (`max_season = current_season - 1`).
   * Consequently, metrics strictly dependent on in-season routes run (Routes Run, Route Participation %, TPRR, YPRR) and snap-level alignment are either **Historical/Offseason only (Class D)** or require a **Paid Provider (Class E)** (such as PFF, FantasyPoints Data, or FTN B2B).
   * **MVP Solution**: Substitute with **snap count / snap share** (`load_snap_counts()`), weekly **targets / target share / air yards / WOPR** (`load_player_stats()`), and passing play volume.

2. **The Power of Open-Source In-Season Data**:
   * `nflverse` provides rich, free, and reliably updated in-season data across three primary functions:
     * `load_pbp()`: Unmatched play-by-play depth (EPA, success, win probability, situational context like down/distance, goal line, red zone, and pre-modeled `pass_oe` / `xpass`).
     * `load_player_stats()`: Pre-computed weekly player aggregates including air yards, air yard share, target share, WOPR, EPA, and fantasy points.
     * `load_pfr_advstats()`: Advanced rushing and passing metrics (yards before/after contact, broken tackles, pressure rates, hurry rates, blitz rates).
     * `load_ftn_charting()`: In-season weekly charting including read progression (`read_thrown`, numeric codes unresolved upstream), play action, RPO, screen passes, motion, and box counts.
     * `load_ff_opportunity()`: Expected fantasy points (xFP) based on situational machine learning models.

---

## Metric Availability Master Table

### Classification Key
* **A**: Easily available and current in-season (pre-aggregated table, weekly updates)
* **B**: Available but requires deriving it from play-by-play (`load_pbp()`)
* **C**: Available only from another free source / separate scraper
* **D**: Historical / offseason only in open source
* **E**: Probably requires a paid provider (PFF, FantasyPoints, SIS, commercial API)

| Metric | Availability | Dataset / Function | Update Frequency | Ingestion Difficulty | Notes & Limitations |
| :--- | :---: | :--- | :--- | :---: | :--- |
| **Player Participation / Usage** | | | | | |
| Offensive snaps | **A** | `nflreadpy.load_snap_counts()` | Weekly (Tue AM) | Low | Sourced from PFR (`offense_snaps`). Total offensive snaps only. |
| Snap share | **A** | `nflreadpy.load_snap_counts()` | Weekly (Tue AM) | Low | Directly provided as `offense_pct` (decimal 0.0–1.0). |
| Routes run | **D / E** | `load_participation()` (D) / Paid API (E) | Post-season only | High / Blocked | FTN participation data is blocked in-season by nflverse contract. |
| Route participation | **D / E** | `load_participation()` (D) / Paid API (E) | Post-season only | High / Blocked | Routes / Team Dropbacks. In-season requires paid data. Use snap share as proxy. |
| Targets | **A** | `nflreadpy.load_player_stats()` | Weekly / Post-game | Low | Official box score target count. Also verifiable via PBP. |
| Target share | **A** | `nflreadpy.load_player_stats()` | Weekly (Tue AM) | Low | Pre-calculated `target_share` = player targets / **team targets** (not pass attempts). **Corrected.** |
| First-read targets | **B** | `load_ftn_charting()` + `load_pbp()` | Weekly / Nightly | Medium | Join on `game_id` and `play_id`. **Unresolved:** the dictionary says `'0'` = primary read, but observed data conflicts (`'1'` on ~50-60% of targets, `'0'` mostly on non-pass plays); pending nflverse/nflreadr#325. Experimental only (contract §3). Not route participation. |
| Air yards | **A** | `nflreadpy.load_player_stats()` | Weekly (Tue AM) | Low | Column `receiving_air_yards`. Includes incomplete passes. |
| Air-yard share | **A** | `nflreadpy.load_player_stats()` | Weekly (Tue AM) | Low | Column `air_yards_share`. Can be volatile on deep targets. |
| ADOT (Avg Depth of Target) | **B** | Derived: `load_player_stats()` | Weekly (Tue AM) | Low | `receiving_air_yards / targets`. Handle `targets == 0` (NULL). |
| Targets per route run (TPRR) | **D / E** | `load_participation()` (D) / Paid API (E) | Post-season only | High / Blocked | Blocked in-season due to missing route counts. |
| Yards per route run (YPRR) | **D / E** | `load_participation()` (D) / Paid API (E) | Post-season only | High / Blocked | Blocked in-season due to missing route counts. |
| Slot / outside alignment | **D / E** | `load_participation()` (D) / Paid API (E) | Post-season only | High / Blocked | Snap-level receiver alignment is strictly historical in free data. |
| **RB Usage** | | | | | |
| Carries | **A** | `nflreadpy.load_player_stats()` | Weekly / Post-game | Low | Official rushing attempts (`carries`). |
| Rushing share | **B** | Derived: `pbp` | Weekly (Tue AM) | Low | **Corrected:** two metrics. `team_rush_share` = player rush plays / team rush plays (scrambles in, kneels/2-pt out); `backfield_rush_share` = RB designed rushes / team RB designed rushes. Note `player_stats.carries` includes kneels. |
| Goal-line carries (inside 5) | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Daily | Low | Filter `rush_attempt == 1 AND yardline_100 <= 5` (excl. kneels/scrambles). |
| Red-zone carries (inside 20) | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Daily | Low | Filter `rush_attempt == 1 AND yardline_100 <= 20`. |
| Routes (RB) | **D / E** | `load_participation()` (D) / Paid API (E) | Post-season only | High / Blocked | Same constraint as WR routes. |
| Targets (RB) | **A** | `nflreadpy.load_player_stats()` | Weekly / Post-game | Low | Standard target count for RB position. |
| Receiving usage (RB) | **A** | `nflreadpy.load_player_stats()` | Weekly / Post-game | Low | Captures receptions, receiving yards, receiving TDs, target share. |
| Third-down usage | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Daily | Medium | Touches & targets isolated where `down == 3`. Full snaps require participation. |
| Two-minute usage | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Daily | Medium | Touches & targets where `half_seconds_remaining <= 120` or `no_huddle == 1`. |
| Yards before contact (YBC) | **A** | `load_pfr_advstats(stat_type="rush")` | Weekly (Tue/Wed) | Low | PFR columns `yds_before_contact` and `ybc_att`. |
| Yards after contact (YAC) | **A** | `load_pfr_advstats(stat_type="rush")` | Weekly (Tue/Wed) | Low | PFR columns `yds_after_contact` and `yac_att`. |
| Missed tackles forced | **A** | `load_pfr_advstats(stat_type="rush")` | Weekly (Tue/Wed) | Low | PFR column `broken_tackles` (also available in `stat_type="rec"`). |
| Rushing success rate | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Daily | Low | Column `success` (1 if play EPA > 0) on rush attempts. |
| Explosive runs (10+ yds) | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Daily | Low | Filter `rush_attempt == 1 AND yards_gained >= 10`. |
| **Team / Offensive Context** | | | | | |
| Play-by-play (PBP) | **A** | `nflreadpy.load_pbp()` | Live / Post-game | Low | 370+ columns including EPA, win prob, clock, down, distance, outcomes. |
| Neutral pass rate | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Weekly | Medium | Pass rate where `wp between 0.20 and 0.80`, `abs(score_diff) <= 7`, outside 2 min. |
| Pass rate over expectation (PROE) | **B** | Pre-modeled in `load_pbp()` | Post-game / Weekly | Low | Pre-calculated column `pass_oe` (or `pass - xpass`). Average across team plays. |
| Pace (sec/play, neutral pace) | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Weekly | Medium | Delta of `game_seconds_remaining` between consecutive snaps in neutral scripts. |
| Personnel groupings (11/12/21) | **D / E** | `load_participation()` (D) / Paid API (E) | Post-season only | High / Blocked | `offense_personnel` is historical only in open source. |
| Play action | **B** | `load_ftn_charting()` + `load_pbp()` | Weekly / Nightly | Low | Column `is_play_action == 1` in FTN charting. |
| Formation / alignment (Shotgun/Pistol) | **B** | `load_pbp()` + `load_ftn_charting()` | Post-game / Weekly | Low | `pbp.shotgun` (0/1); `ftn.qb_location` ('U' under center, 'S' shotgun, 'P' pistol). |
| Red-zone tendencies | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Weekly | Low | Run/pass splits and target allocation inside the 20-yard line. |
| OL metrics (PBWR / RBWR) | **C / E** | ESPN Analytics (Manual) / PFF (E) | Intermittent | High | ESPN publishes PBWR/RBWR articles without an open weekly API. |
| Pressure / sack data | **A / B** | `load_pbp()` + `load_pfr_advstats("pass")` | Weekly | Low | PBP has `sack` & `qb_hit`; PFR has `times_pressured`, `pressure_pct`, `blitzes`. |
| **Defense / Matchup Context** | | | | | |
| Rushing success allowed | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Weekly | Low | Mean of `success` on rushing plays grouped by `defteam`. |
| EPA allowed (overall, pass, rush) | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Weekly | Low | Mean of `epa` grouped by `defteam` split by `pass == 1` vs `rush == 1`. |
| RB targets / rec allowed | **B** | Derived: `load_pbp()` + `load_rosters_weekly()` | Post-game / Weekly | Medium | Count of targets/catches conceded where receiver position is 'RB'. |
| TE targets / rec allowed | **B** | Derived: `load_pbp()` + `load_rosters_weekly()` | Post-game / Weekly | Medium | Count of targets/catches conceded where receiver position is 'TE'. |
| Explosive plays allowed | **B** | Derived: `nflreadpy.load_pbp()` | Post-game / Weekly | Low | Count of passes >= 20 yds or rushes >= 10 yds conceded by `defteam`. |
| Man vs. zone coverage | **E** | PFF / FantasyPoints / SIS | Paid API only | Blocked | Coverage schemes (Cover 1/2/3/4/Man/Zone) are not in free datasets. |
| Defensive personnel (Nickel/Dime/Box) | **D / B** | `load_participation()` (D) / FTN Box (B) | Split | Medium | Nickel/Dime is post-season only. Box count is in-season via `ftn.n_defense_box`. |
| Defensive pressure rate | **B / A** | `load_pbp()` (B) or PFR Def (A) | Weekly | Low | **Corrected:** hit-or-sack = dropbacks with `sack == 1 OR qb_hit == 1`, divided by dropbacks. Never `sacks + hits` (most sacks also carry `qb_hit = 1`). True pressure (incl. hurries) only from PFR. |
| **Other Core Tables** | | | | | |
| Injuries | **A** | `nflreadpy.load_injuries()` | Wed–Sun (multiple/wk) | Low | Official NFL injury reports: practice status (DNP/LP/FP) and game status. |
| Depth charts | **A** | `nflreadpy.load_depth_charts()` | Weekly | Low | Team-issued depth charts (`depth_team` rank: 1 = starter, 2 = backup). |
| Schedules & Vegas odds | **A** | `nflreadpy.load_schedules()` | Full season + Weekly | Low | Contains `spread_line`, `total_line`, weather, roof, gameday. |
| Rosters | **A** | `nflreadpy.load_rosters_weekly()` | Weekly | Low | Active roster status, positions, teams, jersey numbers. |
| Player ID crosswalk | **A** | `nflreadpy.load_ff_playerids()` | Continuous | Low | DynastyProcess mapping across `gsis_id`, `espn_id`, `sleeper_id`, `pfr_id`. |
| Next Gen Stats (NGS) | **A** | `nflreadpy.load_nextgen_stats()` | Weekly (Tue AM) | Low | Passing, rushing, receiving tracking (separation, cushion, RYOE, CPOE). Only qualified players get rows: LEFT JOIN, missing = NULL, never 0. |

---

## BEST MVP METRICS (Top 22)

These metrics are **100% free**, **reliably updated weekly in-season**, **proven to drive fantasy decision-making beyond raw PPG**, and **straightforward to ingest/derive**:

### Player Opportunity & Role
1. **Offensive Snap Share (`offense_pct`)**: Foundational role floor; measures whether a player is on the field for 70%+ vs. 30% of plays.
2. **Target Share (`target_share`)**: The stickiest year-over-year receiver volume metric; independent of game pace.
3. **Air Yard Share (`air_yards_share`)**: Captures downfield target dominance; reveals buy-low candidates with high unrealized air yards.
4. **WOPR (`wopr`)**: Weighted Opportunity Rating (`1.5 * target_share + 0.7 * air_yards_share`). Balances volume and depth into a single score.
5. **Expected Fantasy Points (`xfp`)**: From `load_ff_opportunity()`. Isolates opportunity value from TD variance; identifies buy-low / sell-high value gaps.
6. **Team rush share & backfield rush share** (**Corrected**): `team_rush_share` = share of all team rush plays (scrambles included); `backfield_rush_share` = share of RB designed carries. Only the latter shows true backfield committee splits next to a running QB.
7. **Red-Zone Carries (`rz_carries`)**: Carries inside the 20-yard line; strong leading indicator for rushing touchdowns.
8. **Goal-Line Carries (`gl_carries`)**: Designed carries inside the 5-yard line; a very high-leverage touch. (**Corrected:** the earlier hardcoded "~0.55 expected TDs per carry" figure was unsourced and is not used.)
9. **Red-Zone Targets (`rz_targets`)**: Targets inside the 20-yard line; predicts receiving touchdown equity.
10. **RB Target Share (`rb_target_share`)**: Target share specifically earned by running backs; vital for identifying game-script-immune PPR backs.

### Player Skill & Efficiency (Beyond Box Score)
11. **Yards After Contact per Attempt (`yac_att`)**: From PFR advanced rushing; isolates individual running back talent from offensive line blocking.
12. **Broken Tackles / Missed Tackles Forced (`broken_tackles`)**: From PFR advanced stats; quantifies elusiveness and tackle-breaking ability.
13. **Rushing Success Rate (`rush_success_rate`)**: Percentage of rushes with positive EPA (`success == 1`); measures offensive consistency and chain-moving ability.
14. **Explosive Run Count (10+ yards)**: Measures big-play ceiling for running backs.
15. **Average Depth of Target (`adot`)**: Categorizes receiver role (underneath possession vs. intermediate vs. field-stretcher).

### Offensive & Team Context
16. **Pass Rate Over Expectation (`team_proe`)**: Mean of `pass_oe` from PBP; shows whether a coaching staff leans pass-heavy regardless of game state.
17. **Neutral Pass Rate**: Pass rate when score is within 7 points and win probability is 20%–80%; reveals true offensive identity.
18. **Vegas Implied Team Total**: Derived from `load_schedules()` (`spread_line` and `total_line`); top predictor of weekly offensive touchdown volume.
19. **Team Sacks / Pressures Allowed Rate**: Measures whether a quarterback will be under duress, impacting skill player volume.

### Matchup & Defensive Funnels
20. **Defensive EPA Allowed per Dropback**: Identifies pass defenses that can be exploited for ceiling passing games.
21. **Defensive EPA Allowed per Rush**: Identifies run defenses susceptible to heavy ground games.
22. **Defensive Target Funnel to RBs / TEs**: Opponent targets to RBs/TEs conceded by a defense; highlights specific weekly streaming advantages.

---

## DON'T WASTE TIME ON THESE YET (Defer to V2)

Avoid spending development resources on these metrics for the MVP:

1. **Routes Run, Route Participation %, TPRR, YPRR**:
   * *Why defer*: nflverse participation data is legally locked to post-season releases (`max_season = current - 1`). No free API provides weekly in-season route counts. Scraping consumer websites is brittle and unmaintainable.
   * *MVP Substitute*: Use `load_snap_counts()` (`offense_pct`) and total team dropbacks as a role proxy.
2. **Receiver Alignment (Slot vs. Outside Splits)**:
   * *Why defer*: Requires play-by-play snap alignment charting, which is paywalled (PFF / FantasyPoints) or delayed to offseason in nflverse.
3. **Man vs. Zone Coverage Splits**:
   * *Why defer*: Coverage scheme tagging is completely absent from open-source data. It requires proprietary human charting feeds (PFF, SIS, FantasyPoints).
4. **Defensive Personnel Formations (Nickel, Dime, Base 4-3/3-4)**:
   * *Why defer*: Located in `load_participation()`, which does not update in-season. FTN provides box counts (`n_defense_box`), but full secondary packages are unavailable.
5. **ESPN Pass Block Win Rate (PBWR) & Run Block Win Rate (RBWR)**:
   * *Why defer*: ESPN publishes these sporadically in narrative articles and editorial tables. There is no automated, versioned JSON or Parquet pipeline.
   * *MVP Substitute*: Use `load_pfr_advstats(stat_type="pass")` pressure rate and PBP sack rates.
6. **Next Gen Stats for Rotational / Handcuff Players**:
   * *Why defer*: NFL Next Gen Stats enforces strict minimum attempt/target thresholds before publishing a player's row. Rotational backs, WR4s, and backup TEs simply disappear from the table, creating severe null-handling bugs.
   * *Recommendation*: Use NGS only as a secondary enrichment table for qualified starters; rely on PBP/PFR for universal player coverage.

---

## DERIVED METRICS: Logic & Source Columns

*Note: Formulas below specify exact logic and source columns for DuckDB or Polars implementation without application code.*

### 1. Goal-Line Carries (GL Carries)
* **Goal**: Measure high-leverage rushing opportunities inside the 5-yard line.
* **Source Table**: `nflreadpy.load_pbp()`
* **Filter Conditions**:
  * `rush_attempt == 1`
  * `yardline_100 <= 5`
  * `qb_kneel == 0`
  * `qb_scramble == 0`
  * `two_point_attempt == 0`
* **Aggregation**: `COUNT(*) GROUP BY season, week, rusher_player_id`

### 2. Red-Zone Carries & Targets (RZ Carries, RZ Targets)
* **Goal**: Measure scoring-position opportunity volume.
* **Source Table**: `nflreadpy.load_pbp()`
* **Filter Conditions** (**Corrected**: designed rushes only, so QB scrambles don't count):
  * Carries: `play_type == 'run' AND qb_kneel == 0 AND qb_scramble == 0 AND two_point_attempt == 0 AND yardline_100 <= 20`
  * Targets: `pass_attempt == 1 AND yardline_100 <= 20 AND two_point_attempt == 0`
* **Aggregation**: 
  * Carries: `COUNT(*) GROUP BY season, week, rusher_player_id`
  * Targets: `COUNT(*) GROUP BY season, week, receiver_player_id`

### 3. Rushing Share (**Corrected**: split into two metrics)
* **team_rush_share** = player rush plays / team rush plays, where a rush play is
  `play_type == 'run' AND qb_kneel == 0 AND two_point_attempt == 0` (scrambles included).
* **backfield_rush_share** (RBs only) = player designed rushes (`qb_scramble == 0`) /
  team designed rushes by RBs.
* Do not use `player_stats.carries` as the numerator for shares: it includes kneels.

### 4. Average Depth of Target (ADOT)
* **Goal**: Characterize receiving depth profile.
* **Source Table**: `nflreadpy.load_player_stats()`
* **Formula**:
  $$\text{ADOT} = \begin{cases} \frac{\text{receiving\_air\_yards}}{\text{targets}}, & \text{if } \text{targets} > 0 \\ \text{NULL}, & \text{otherwise} \end{cases}$$
* **Source Columns**: `receiving_air_yards`, `targets`

### 5. Team Pass Rate Over Expectation (PROE)
* **Goal**: Evaluate offensive gameplan aggressiveness independent of score and game clock.
* **Source Table**: `nflreadpy.load_pbp()`
* **Filter Conditions**:
  * `play_type IN ('pass', 'run')`
  * `qb_kneel == 0`
  * `qb_spike == 0`
  * `half_seconds_remaining > 120`
  * `pass_oe IS NOT NULL`
* **Formula**:
  $$\text{team\_PROE} = \text{AVG}(\text{pass\_oe}) \quad \text{or} \quad \text{AVG}(\text{pass} - \text{xpass}) \times 100$$
* **Source Columns**: `pass_oe`, `xpass`, `pass`, `posteam`, `season`, `week`

### 6. Team Neutral Pass Rate
* **Goal**: Measure true offensive baseline tendency when game script does not force pass or run.
* **Source Table**: `nflreadpy.load_pbp()`
* **Filter Conditions**:
  * `abs(score_differential) <= 7`
  * `wp BETWEEN 0.20 AND 0.80`
  * `half_seconds_remaining > 120`
  * `qb_kneel == 0`
  * `play_type IN ('pass', 'run')`
* **Formula**:
  $$\text{neutral\_pass\_rate} = \frac{\text{COUNT}(\text{pass} == 1)}{\text{COUNT}(\text{pass} == 1 \text{ OR } \text{rush} == 1)}$$
* **Source Columns**: `pass`, `rush`, `score_differential`, `wp`, `half_seconds_remaining`, `posteam`

### 7. Explosive Run Rate & Explosive Pass Rate
* **Goal**: Quantify offensive big-play creation and defensive big-play vulnerability.
* **Source Table**: `nflreadpy.load_pbp()`
* **Logic**:
  * Explosive Run: `rush_attempt == 1 AND yards_gained >= 10`
  * Explosive Pass: `pass_attempt == 1 AND yards_gained >= 20`
* **Formula**:
  $$\text{explosive\_run\_rate} = \frac{\text{COUNT}(\text{rush\_attempt} == 1 \text{ AND } \text{yards\_gained} \ge 10)}{\text{COUNT}(\text{rush\_attempt} == 1)}$$
* **Source Columns**: `rush_attempt`, `pass_attempt`, `yards_gained`, `rusher_player_id`, `posteam`, `defteam`

### 8. Rushing Success Rate
* **Goal**: Measure ball-carrier efficiency using contextual down and distance.
* **Source Table**: `nflreadpy.load_pbp()`
* **Logic**: `load_pbp()` natively computes `success` (1 if EPA > 0 on the play, 0 otherwise).
* **Formula**:
  $$\text{rushing\_success\_rate} = \frac{\text{COUNT}(\text{rush\_attempt} == 1 \text{ AND } \text{success} == 1)}{\text{COUNT}(\text{rush\_attempt} == 1)}$$
* **Source Columns**: `rush_attempt`, `success`, `rusher_player_id`

### 9. Third-Down Touch & Target Involvement
* **Goal**: Identify passing-down backs and clutch chain-moving receivers without full participation data.
* **Source Table**: `nflreadpy.load_pbp()`
* **Filter Conditions**: `down == 3 AND play_type IN ('pass', 'run')`
* **Formula**:
  $$\text{third\_down\_opp\_share} = \frac{\text{COUNT}(\text{rusher\_player\_id} == \text{id} \text{ OR } \text{receiver\_player\_id} == \text{id})}{\text{COUNT}(\text{team plays on 3rd down})}$$
* **Source Columns**: `down`, `rusher_player_id`, `receiver_player_id`, `posteam`

### 10. Defensive EPA Allowed per Play (Pass vs. Rush)
* **Goal**: Benchmark defensive matchup difficulty.
* **Source Table**: `nflreadpy.load_pbp()`
* **Logic**:
  * Pass Defense EPA: `AVG(epa) WHERE pass == 1 GROUP BY defteam`
  * Rush Defense EPA: `AVG(epa) WHERE rush == 1 GROUP BY defteam`
* **Source Columns**: `epa`, `pass`, `rush`, `defteam`, `season`, `week`

### 11. Defensive Target Funnel by Position (RB / TE Targets Allowed)
* **Goal**: Identify defenses that surrender disproportionate passing volume to secondary positions.
* **Source Table**: Join `nflreadpy.load_pbp()` with `load_rosters_weekly()` on `pbp.receiver_player_id == roster.gsis_id`
* **Formula**:
  $$\text{position\_target\_share\_allowed} = \frac{\text{COUNT}(\text{pass\_attempt} == 1 \text{ AND } \text{roster.position} == \text{'TE'})}{\text{COUNT}(\text{pass\_attempt} == 1 \text{ against defteam})}$$
* **Source Columns**: `pbp.receiver_player_id`, `pbp.pass_attempt`, `pbp.defteam`, `roster.position`

### 12. Vegas Implied Team Total
* **Goal**: Project baseline scoring environment and touchdown volume per game.
* **Source Table**: `nflreadpy.load_schedules()`
* **Formulas** (**Corrected** sign; verified against nflreadr dictionary and game results):
  * Spread convention in nflverse: a **positive** `spread_line` means the **home** team is favored.
  * $\text{home\_implied\_total} = \frac{\text{total\_line} + \text{spread\_line}}{2}$
  * $\text{away\_implied\_total} = \frac{\text{total\_line} - \text{spread\_line}}{2}$
* **Source Columns**: `total_line`, `spread_line`, `home_team`, `away_team`, `season`, `week`

---

## Data Ingestion & Engineering Recommendations

1. **Storage Format**:
   * Ingest data using Polars directly into DuckDB tables partitioned by `season`.
   * PBP tables are large (~50,000 plays per season, 370+ columns). Rather than querying all columns in the UI, write a weekly transformation pipeline in DuckDB that aggregates PBP into summary tables:
     * `player_weekly_usage` (combining `player_stats`, `snap_counts`, and PBP goal-line/red-zone metrics)
     * `team_weekly_context` (pace, PROE, neutral pass rate, total plays)
     * `defensive_weekly_matchups` (EPA allowed, success allowed, RB/TE targets conceded)
2. **Player ID Unification**:
   * Use `load_ff_playerids()` as the primary identity bridge.
   * `gsis_id` is the primary key across `nfl_players`, `pbp`, `player_stats`, and `nextgen_stats`.
   * `espn_id` connects to ESPN fantasy league rosters.
   * `pfr_id` connects to `load_pfr_advstats()` (yards after contact, broken tackles, pressure metrics).
3. **Weekly Sync Schedule**:
   * **Tuesday Morning (3:00–6:00 AM ET)**: Full weekly refresh of `pbp`, `player_stats`, `snap_counts`, `nextgen_stats`, and `ff_opportunity`.
   * **Wednesday Afternoon**: First weekly practice report update via `load_injuries()`.
   * **Friday Afternoon & Sunday 90 min before kickoff**: Game status injury updates (Active/Inactive, Questionable, Out).

# #254: actual-scoring support table (phases 1 and 2)

This table covers which fantasy-point numbers are **truly in a league's own scoring**, where each number comes from,
and which analysis tools read it. It covers QB, RB, WR, TE, K and D/ST, for ESPN and Sleeper, per rule category.
It meets #254 acceptance item 1. Decision D-12 (`docs/roadmap-execution/DECISIONS.md`) sets the ownership: #253 owns
diagnostics and the contract, and #254 owns scoring fidelity.

Paths are relative to `fantasy-intel/backend/`. Sections 1-6 were written for phase 1 (PR #259, merged as `261728e`);
"this PR" in them means phase 1. Their line numbers were taken at `e11ff5d` plus phase 1's `rescore.py` and have moved
since; where they point into code changed later they are replaced by function names, which stay valid. Section 7 covers
phase 2 and names functions, not lines. Each claim is marked as follows:

- **test**: pinned by a test in this repository.
- **code**: read from the source and not executed.
- **UNVERIFIED**: an inference that still needs a fixture or real-payload check.

All fixtures are synthetic. No real league, credential or provider endpoint was used.

Status words follow `analytics/league_rules.py`: `handled`, `partial`, `unsupported` and `unverified`.

---

## 1. The three scoring sources

Every per-player fantasy number in the app comes from one of three sources. These must never be presented as each
other.

| Source | What it is | Where it is written | League-scored? |
|---|---|---|---|
| **P: provider points** | ESPN `appliedTotal` (actual: `statSourceId 0`; projection: `statSourceId 1`), and Sleeper `players_points` (actual only) | ESPN: `espn/parse.py:189-198`, `:308-311` into `espn_player_weekly_points` (`db/schema.sql:122-133`). Sleeper: `sleeper/write.py:262-265`, `:228-232` | **Yes, by the provider.** ESPN computes both actuals and projections with the league's own rules, including rules Fantasy Intel cannot model (TE slot premium, bonuses, IDP). Sleeper gives actuals only; `projected_points` is always NULL (`sleeper/write.py:13-15`, `:230`). |
| **O: independent Ours (skill)** | The `ifp` model's QB/RB/WR/TE projections. **Copied** when the league's skill fingerprint equals the reference (laptop) scoring. Otherwise **re-scored** by a box-score mix ratio (`rescore.py:241-262`, flag `RESCORED_FLAG`, `evaluation.rescored_from.method = "stat-mix ratio"`). | `analytics/rescore.py` from the global file's `ref_*` tables (`db/league_split.py:63`) | **Exact only on a fingerprint match.** A re-score is an **approximation**: it scales the reference projection by `points_league(mix) / points_ref(mix)`, where the mix is the player's last 8 games (`rescore.py:71-106`, `:113-150`). The model was not trained in the league's units. |
| **K / D: kicker and D/ST models** | `kfp` (kicker) and `dfp-r1` (D/ST) runs | `rescore.py:264-301` | **Exact for supported rules.** Both scorings are linear in the stored expected statistics (`stat_line.scoring_stats`), so a re-score is exact (`kicker_scoring.py:172-181`, `dst_store.py:120-124`). A run whose rows cannot answer a rule exactly is not re-scored (`rescore.py:154-176`, `:183-192`). |

Two further numbers are **not league-scored** and are labelled as such elsewhere:

- **xFP and usage percentiles** (waiver player value) are full PPR. See S2.
- **Sleeper free-agent "actual" points** are a Fantasy Intel box-score rescore stored in the provider column. Since
  phase 2 each row names its source (`actual_source`). See S3 and section 7.3.

### How a missing Ours number is shown

`projection_source.weekly` and `league_projections` replace the provider number only for rows that are
`projected` (`projection_source.py:306-311`, `:138-143`). They also replace bye or withheld rows with a measured 0
(`ours_zero`). Any other row keeps the provider value with origin `espn`. This includes `insufficient_data`, which is
what a re-score that abstains produces.

- **ESPN league:** an abstained re-score shows ESPN's own league-scored projection, labelled as ESPN's (**code**).
- **Sleeper league:** there is no provider projection, so the value is `None` (missing, never 0) (**code**).

---

## 2. Support table: rule category by position by provider

Columns:

- **ESPN P / Sleeper P**: provider numbers.
- **O**: independent Ours.
- **K/D**: the kicker and D/ST models.
- **Tools**: the main consumers.

"Abstains" means the family produces no league-unit number and the tools fall back as described in section 1.

### 2.1 QB / RB / WR / TE (skill)

| Rule category | ESPN P | Sleeper P | O (independent) | Source columns | Tools |
|---|---|---|---|---|---|
| Yardage, TDs, receptions (PPR, half, standard), 2-pt, INT thrown, fumbles lost, fumble-recovery TD | league-scored actual and projection | league-scored actual (rostered weeks) | **handled**: exact on a fingerprint match. Otherwise a ratio re-score, which is an approximation (S1). Mapping: `league_scoring.py:32-46`, Sleeper `sleeper/scoring.py:30-38`. Exact re-score outcomes: **test** (`test_rescore.py`, half PPR, 6-point pass TD, unusual QB). | nflverse `nfl_weekly_stats` `MIX_COLUMNS` (`league_scoring.py:60-74`) | start_sit, lineup what-if, team_lineups, trades, trade discovery, trade_assets, add/drop, team_value, power rankings, playoff race, rankings (Ours opt-in), waivers forward effect (`waiver_data.py:107-145`) |
| **Unusual QB scoring** (6-point pass TD, per-yard values, INT penalties, per-completion and per-attempt points) | league-scored | league-scored actual | **handled** for completions and attempts (`EXTRA_DIRECT` 2004-2006, `league_scoring.py:66-73`), but **only by re-score** (`MIX_COLUMNS`, not `COMPONENT_COLUMNS`). Exact outcome: **test** (`test_unusual_qb_scoring_rescores_exactly`). | as above | as above |
| First downs (pass, rush, rec) | ESPN has no stat id | league-scored actual | re-score only (ids 2001-2003). This PR makes the mix require a known first-down count (section 3). Whether real nflverse rows ever carry NULL first downs is **UNVERIFIED**. | `*_first_downs` | as above |
| Kick and punt return TDs (player) | league-scored | league-scored | **handled** when both are worth the same. Otherwise **unsupported**, because nflverse combines them (`league_scoring.py:178-184`). | `special_teams_tds` | as above |
| **Negative modifiers** (INT, fumbles lost, per-attempt) | league-scored | league-scored | **handled**: linear weights. Exact outcome: **test** (`test_negative_modifiers_rescore_exactly_and_a_negative_result_keeps_an_ordered_range`). A re-score whose ratio is negative now keeps `interval_low <= interval_high` (this PR). The additive shift path (reference mix under 1 point) clamps at 0 (`rescore.py:140-144`): a known limit (**code**). | as above | as above |
| Zero result (the league scores none of the player's statistics) | league-scored | league-scored | **handled**: a computed `0.0` with `coverage_status = projected`, a value and not a gap. **test** (`test_a_scoring_that_awards_nothing_for_his_statistics_gives_a_computed_zero`). | as above | as above |
| **TE premium as a Sleeper position bonus** (`bonus_rec_te`, RB and WR likewise) | n/a | league-scored | **handled** by re-score (`positionPoints`, `league_scoring.py:157-163`, `sleeper/scoring.py:93-94`). **test** (`test_sleeper_scoring.py::test_a_te_premium_rescores_tight_ends_and_leaves_receivers_alone`). xFP and ratings ignore it, and `league_rules` says `partial` (`league_rules.engine_support`, row `te_premium`). | as above | as above; waivers value is full PPR (S2) |
| **TE premium / per-slot override (ESPN `pointsOverrides` on a non-D/ST slot)** | league-scored | n/a | **unsupported**: Ours abstains for the whole league (`league_scoring.py:128-136`). ESPN's own numbers are used, labelled ESPN. **test** (`test_league_rules.py` TE slot override fixture). No algorithm change in #254 phase 1. | none | all of the above show ESPN numbers. `league_rules` gives "TE premium" `partial` and "Ours" `unsupported`. |
| **Bonuses** (ESPN yardage or 100/300-yard bonuses; Sleeper `bonus_pass_yd_300`, `bonus_rush_yd_100`, ...) | league-scored | league-scored actual | **unsupported**: any non-zero offensive rule that is not implemented abstains (`league_scoring.py:175-177`). Sleeper keys are translated to `SKILL_UNSUPPORTED_ID` (`sleeper/scoring.py:63`, `:119-120`). **test** (`test_rescore.py::test_an_unsupported_scoring_gets_no_skill_runs_and_says_why`). | none | ESPN: provider numbers. Sleeper: no projections at all (missing). Start/Sit labels it per #253 (S8, PR #256). |
| Reverse scoring items | league-scored | n/a | **unsupported** (`league_scoring.py:149-151`) | none | as bonuses |
| **Unknown stat ids** (a non-zero ESPN id outside every mapped set) | league-scored | n/a | **unsupported** for skill (`league_scoring.py:175-177`). The ESPN adapter lists it as unmapped (`leagues/providers/espn.py:106-140`). | none | `league_rules` lists it under `unmapped` |
| Defensive-return events on a skill player (93, 103, 104, 206, 209) | league-scored | league-scored | **partial**: `unmodeled`, so the effect is measured by reconciliation and never assumed to be zero (`league_scoring.py:57-58`, `:173-174`) | none | — |
| Sleeper special-teams fumbles for a player (`st_ff`, `st_fum_rec`) | n/a | league-scored | not projected, and listed in translation notes (`sleeper/scoring.py:103-104`). **partial**, **UNVERIFIED** effect. | none | — |
| **IDP** (individual defensive players) | league-scored, but IDP players are not imported as projectable | Sleeper `idp_*` keys are listed as unsupported and ignored (`sleeper/scoring.py:111-112`) | **unsupported / gated**: no IDP model. ESPN: an IDP stat id with non-zero base points falls into the "not implemented" branch and makes **all** skill Ours abstain (`league_scoring.py:175-177`; **code**, **UNVERIFIED** against a real ESPN IDP payload). | none | #253 diagnostics (G2-G4) |
| **Missing box-score component** (a NULL in a scored column) | n/a | n/a | **Fixed in this PR (S1b)**: the component is unknown and the row abstains (section 3). **test**. | — | — |

### 2.2 K (kicker)

| Rule category | ESPN P | Sleeper P | K model | Tools |
|---|---|---|---|---|
| FG distance buckets: ESPN 0-39, 40-49, 50-59, 60+, 50+, any; made, attempted and missed (`kicker_scoring.py:46-62`) | league-scored | n/a | **handled**: exact from expected statistics (`kicker_scoring.py:172-181`). **test** (`test_kicker_projections.py::test_scoring_stats_rescore_exactly_to_the_models_own_points`). Overlapping 50+ and 50-59 items are additive, with a note (`kicker_scoring.py:219-222`). | start_sit, team_lineups-based tools, kdst_streaming, rankings (via `projection_source._kicker_overlay`) |
| Sleeper finer buckets (0-19, 20-29, 30-39 as synthetic 1080-1085) | n/a | league-scored actual | **handled** for runs that store the finer buckets. A run with only the coarse 0-39 bucket **abstains** for a scoring that splits it (`kicker_scoring.py:177-178`; `rescore.py` skips the run). **test** (`test_kicker_projections.py::test_a_row_with_only_the_coarse_bucket_cannot_be_rescored_to_a_split_scoring`). | as above |
| PAT made, attempted and missed | league-scored | league-scored actual | **handled** | as above |
| Per-FG-yard points and other kicker rules not implemented (Sleeper `fgm_yds` and similar) | league-scored | league-scored actual | **unsupported**: Sleeper prefix keys map to `KICKER_UNSUPPORTED_ID` (`sleeper/scoring.py:115-116`), and ESPN unknown ids to "not implemented" (`kicker_scoring.py:216-218`). Ours abstains. No algorithm change in phase 1. | ESPN: provider K numbers. Sleeper: K missing, which makes every team-week with a K known-only (G10). |
| Fake-kick offense and return TDs on a K | league-scored | league-scored | **partial**: unmodeled (`kicker_scoring.py:211-215`) | — |
| League scores no FG at all | — | — | **unsupported** (`kicker_scoring.py:223-224`) | — |

### 2.3 D/ST

| Rule category | ESPN P | Sleeper P | D/ST model | Tools |
|---|---|---|---|---|
| Sacks, INT, fumble recoveries, blocks, safeties, return TDs, def 2-pt, 1-pt safety (`dst_research.py:60-74`) | league-scored | league-scored actual | **handled**: exact from expected statistics (`dst_store.py:120-124`). **test** (`test_dst_production.py::test_scoring_stats_rescore_exactly...`). | start_sit, team_lineups-based tools, kdst_streaming (via `_dst_overlay`) |
| **Points-allowed tiers**: ESPN 89-92, 121-125; Sleeper 0, 1-6, 7-13, 14-20, 21-27, 28-34, 35+ (synthetic 1089-1095) | league-scored | league-scored actual | **handled** as tier probabilities (`dst_research.py:76-89`). **This PR:** a stored row with no expectation for a tier the league scores (a run stored before the Sleeper tiers were recorded) is no longer read as probability 0; the run is not re-scored (`rescore.py:183-188`). **test** (`test_a_dst_row_missing_a_scored_statistic_is_not_rescored`). | as above |
| Yards-allowed tiers (ESPN 128-136; Sleeper `yds_allow_*` mapped onto them, `sleeper/scoring.py:57-59`) | league-scored | league-scored actual | **handled** for ESPN (tiers verified against ESPN indicators, `dst_research.py:76`). For Sleeper, whether its boundaries (for example `yds_allow_0_100` against ESPN's `<= 99`) match exactly is **UNVERIFIED**. | as above |
| ESPN's alternative 187-196 points-allowed family, tackles, return yards, ESPN forced fumbles (106) | league-scored | n/a | **unsupported**: in `DST_UNIVERSE` but not `MODELED`, so the model abstains (`dst_store.py:94-108`, `dst_research.py:86-95`) | ESPN D/ST numbers |
| Sleeper `ff` (forced fumbles, synthetic 1106) | n/a | league-scored actual | **partial**: approximated from fumble recoveries, never validated (`dst_research.py:86-89`, `sleeper/scoring.py:108-110`) | as above |
| Sleeper special-teams fumbles (`def_st_ff`, `def_st_fum_rec`) with a different value | n/a | league-scored actual | **partial**: approximated by the defensive key and noted (`sleeper/scoring.py:98-102`) | — |

### 2.4 Consumers whose basis is not league scoring

| Tool | Basis | Status | Ref |
|---|---|---|---|
| waivers player value (S2) | xFP or usage percentile (full PPR), falling back to the recent actual-points percentile | **partial**. The basis is still full PPR. Since #260 the waiver warning fires for every rule that differs from the PPR basis (`waiver_data.ppr_basis_differences`: reception points, pass-TD and other basis rules, TE premium, first downs and attempts), naming the difference, and the diagnostic reports `player_value_basis` `partial`. The basis itself is open (section 7.6). | `waivers.BASIS_TEXT`, `waivers.load_values`; `waiver_data.load_waiver_context` |
| `league_rules` "Ours" engine row (S1 label) | a ratio re-score is labelled an approximation | **partial** since #256 (merged): `league_rules.skill_runs_rescored` makes the `skill_projections` row `partial` with `RESCORED_DETAIL`, and the diagnostic's finding carries `{"follow_up": "#254"}`. | `league_rules.engine_support`, `league_rules.skill_runs_rescored` |
| team_lineups `source_label` | named "Ours" for a Sleeper league even when no Ours run exists (S8 outside Start/Sit) | **fixed in phase 2** for add/drop, K/D/ST streaming, power rankings, team value and the replacement market: "Unavailable" when the state holds no number. Trade evaluation and trade alternatives still call it without the values (section 7.6). | `team_lineups.source_label` |

---

## 3. S1b fix in this PR: a missing component is unknown, never 0

**Bug.** `stat_mix` averaged every component as `avg(coalesce(c, 0))`. A NULL box-score value (for example a first-down
count missing for one game) entered the mix as a measured 0, and the ratio re-score produced a number from it.

**Fix** (`rescore.py:71-106`, `:113-130`):

- **Player mix.** A component is averaged only when every game in the player's window has it
  (`count(c) = count(*)`). Otherwise it is `None`.
- **Position mix** (used for players with fewer than 3 games). Each component is averaged over the players who have
  it. It is `None` when none do.
- **Row.** When the reference or target scoring weights a component that is `None` or absent, the row becomes
  `insufficient_data` with `MISSING_COMPONENT_FLAG`, distinct from `NO_MIX_FLAG` (no mix at all). The consumers then
  show the provider number, labelled as such, or missing (section 1). A NULL in a column neither scoring weights does
  not block the re-score.
- **D/ST analogue** (`rescore.py:183-188`). `dst_store.expected_points` read an absent expected statistic as 0. The
  re-score now skips a run whose rows lack a statistic the league scores, as the kicker path already did for coarse
  buckets.
- **Interval order.** A negative ratio now keeps `interval_low <= interval_high`.

**Provider and independent stay distinct.** Abstained rows never receive a number. Re-scored rows keep
`RESCORED_FLAG` and `evaluation.rescored_from`. Provider values keep origin `espn` in `projection_source`.

**Goldens.** No golden changed. The E2E fixture and per-league goldens contain no NULL mix components, and the
reference league's runs are copied verbatim (`test_golden_legacy.py`, `test_league_scope.py` and
`test_league_scope_sleeper.py` pass unchanged).

---

## 4. S3: Sleeper `actual_points` provenance (phase 1 analysis; implemented in phase 2, section 7.3)

`espn_player_weekly_points.actual_points` in a Sleeper league mixes two sources in one column:

1. **Sleeper's `players_points`** for player-weeks on a roster (`sleeper/write.py:262-265`, written at `:228-233`).
2. **A Fantasy Intel box-score rescore** for the others (`_box_score_points`, `sleeper/write.py:365-386`). This covers
   QB/RB/WR/TE only, and only when the league's skill scoring is supported. A NULL component gives NULL points, so it
   is not counted as 0 (`league_scoring.points_sql`, `:203-218`). Unmodeled events (defensive returns, special-teams
   fumbles) are absent from the rescore, so for those events it equals 0 (**code**).

No per-row field records which source a row came from. Readers that mix the sources:

- the waivers fallback percentile (`waivers.py:443-450`);
- `player_compare.py:483-485`;
- `weekly_recap.py:139`;
- `usage.py:111`;
- the legacy player route (`api/routes.py:286`).

**Why it is not fixed here.** An additive `actual_source` column is possible (`schema.sql` already uses
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, for example `:634-637`). It would touch the shared schema of a table that
every league file and many readers use. On its own it would change nothing a user sees, because the readers above
would also need to surface or filter it. That is more than a small additive per-row field, so it is **phase 2**.

---

## 5. Unknowns (labelled honestly)

- How often real nflverse `load_player_stats` rows carry NULL in `MIX_COLUMNS` (first downs, 2-pt,
  `fumbles_lost_total`, `special_teams_tds`), including across seasons loaded from different releases:
  **UNVERIFIED**. `app/nfl/sync.py` does not fill nulls for `nfl_weekly_stats`. This PR makes the result correct in
  either case.
- ESPN IDP and bonus stat ids in real league payloads: **UNVERIFIED**. The resolvers abstain on any id they do not
  implement (**code**).
- The real FX-17 league's scoring modifiers: **unknown** (D-12). Nothing here implies them.
- Sleeper yards-allowed tier boundaries against ESPN's: **UNVERIFIED** (section 2.3).
- The accuracy of the ratio re-score itself, a model trained in reference units and scaled: **unverified**. No
  backtest per target scoring exists. This is the reason S1 is labelled `partial` (#256, merged).
- Cross-league fingerprint and cache isolation (`scoring_fingerprint`, `LeagueScope.cache_key`): audited in phase 2
  (section 7.5).

---

## 6. Recommended phase 2 (as written after phase 1; status in section 7)

1. **S3 provenance:** add `actual_source` (`provider` or `box_score`) to `espn_player_weekly_points` via `ADD COLUMN IF
   NOT EXISTS`. Set it in `sleeper/write.py`, and have the waivers fallback and player_compare surface or filter it.
2. **Re-score accuracy evidence:** a per-target-scoring backtest of the ratio re-score (half PPR, 6-point pass TD,
   first downs) against box-score outcomes. Until it exists, keep the `partial` label from #256.
3. **Additive shift path:** decide whether the `max(..., 0)` clamp (`rescore.py:140-144`) should allow negative
   projections, consistent with the ratio path.
4. **S2 basis:** the warning part was done by #260 (it fires for every rule that differs from the PPR basis). What
   remains is the basis itself: waiver values in league points instead of full-PPR percentiles.
5. **Fixtures for acceptance items 2 and 3 across tools:** multiple FLEX slots, and a TE slot-override or kicker
   per-yard league through start_sit, trades and the waiver planner. Assert abstention or provider labelling end to
   end, reusing #253's `compat_fixtures.py` once it merges.
6. **Cache and fingerprint isolation audit** (acceptance item 6).

---

## 7. Phase 2 (S1b closure, L2, S3, rule ids in the diagnostic, cache and fingerprint audit)

Tests are in `tests/test_scoring_fidelity.py` unless another file is named. All fixtures are synthetic.

### 7.1 S1b: every league-scored independent number built from missing components abstains

| Path | Missing input | Outcome | Since |
|---|---|---|---|
| Skill ratio re-score (`rescore.stat_mix`, `rescore_skill_rows`) | a box-score component NULL in any game of the window | row `insufficient_data` + `MISSING_COMPONENT_FLAG` | phase 1 (`test_rescore.py`) |
| D/ST re-score (`rescore._dst_points`) | an expected statistic the league scores | run not re-scored | phase 1 (`test_rescore.py`) |
| **Kicker re-score** (`kicker_scoring.expected_points`) | an outcome (made, missed, blocked) or distance bucket the league scores, or a scored PAT outcome | `None`, so `rescore_kicker_rows` skips the run. Before, an absent outcome added nothing (a measured 0). | **phase 2**: `test_a_kicker_row_without_a_scored_outcome_is_unknown_never_zero` |
| Kicker coarse bucket for a split scoring | 0-19 / 20-29 / 30-39 detail | `None` (unchanged) | `test_kicker_projections.py` |
| Box-score scoring (`league_scoring.points`, `points_sql`) | any weighted component | `None` / SQL NULL (unchanged) | `test_unusual_qb_scoring_zero_and_negative_box_scores_score_exactly` |
| Sleeper box-score actual points (`sleeper/write._box_score_points`) | a weighted component | `actual_points` NULL **and no `actual_source`** | **phase 2**: `test_sleeper_actual_points_name_their_source_per_row` |

`dst_store.expected_points` still reads an absent statistic as 0, but the only path that computes points from stored
expectations, `rescore._dst_points`, checks every scored statistic first; served D/ST numbers are the stored
`projected_points`. Two modelling choices remain and are not missing-as-zero: the additive shift path for a reference
mix under 1 point clamps at 0, and the lineup engine fills a slot with a negative projection when no better eligible
player exists (pinned exactly in the FX-17 test below; leaving a slot empty would score 0).

**Exact outcomes for supported rules** (acceptance item 2): `test_unusual_qb_scoring_zero_and_negative_box_scores_score_exactly`
(6-point pass TD, 0.05/yd, -3 INT, +0.5 completion, -0.25 attempt: 27.2; a computed 0.0; -10.5; a missing completion
count is `None`) and `test_fx17_three_flex_and_op_lineup_total_is_exact_with_zero_and_negative_projections` (17 distinct
starters, three FLEX, a -0.5 OP and a -2.0 K, a 0.0 D/ST: 130.2, equal to the sum of the starters). Phase 1's re-score
cases stay in `test_rescore.py`.

**No fabricated independent results** (acceptance item 3): an ESPN TE slot premium and an unimplemented ESPN statistic
leave the league with no skill runs and the switch unavailable (only provider numbers, labelled `espn`); a Sleeper
per-yard kicker rule makes the kicker model abstain; Sleeper IDP keys are listed and never change the skill or D/ST
fingerprint, so no IDP number exists and offensive projections are unaffected
(`test_a_te_slot_premium_or_an_unimplemented_stat_gets_no_independent_skill_numbers`,
`test_sleeper_kicker_per_yard_and_idp_rules_never_feed_a_model`).

### 7.2 L2: a source is named only for the numbers it supplied

Under `source=independent` the switch fills the numbers the models abstain on with the provider's (origin `espn`).

| Surface | Before | Phase 2 |
|---|---|---|
| Start/Sit `projection_source` (`start_sit.projection_source_label`) | "Independent model" whenever a verified run exists, even when every shown number is ESPN's fallback (for example a re-score that abstained on a missing component) | "ESPN" when no number shown for the team's own roster came from a model (`projection_source.MODEL_ORIGINS`; another team's numbers never count); rows keep their own `projection_origin` |
| team_lineups tools (`team_lineups.source_label`): add/drop, K/D/ST streaming, power rankings, team value, replacement market | "Ours" for a Sleeper league without any Ours number | "Unavailable" when the state holds no number; the add/drop and streaming pages read it as "no projections" (`labels.projectionsPhrase`), and the K/D/ST streaming provenance decides Ours or ESPN from the league, never from this label. Under the league default an Ours state never holds a provider number: a league defaults to Ours only when its provider publishes none (`projected_points` is always NULL for Sleeper). |
| Stored rest-of-season rows (`player_ros_projections.projection_source`, `projections.build_ros_projections`) | "Ours" on every row of an Ours league, including rows without a number | each row names the source of its own numbers (`projection_source.served_label`: "Ours", "ESPN", "Ours and ESPN", or "Unavailable") |
| Matchup, Start/Sit rows, forward view, trade values board | per-row origin already | unchanged |
| Rankings (Ours basis) | ranks Ours values only; never fills with ESPN | unchanged |

Tests: `test_served_label_names_only_sources_that_supplied_numbers`,
`test_start_sit_names_espn_when_every_shown_number_is_the_providers_fallback`,
`test_team_lineup_tools_never_name_ours_over_a_state_without_numbers` (M3: every stored row "Unavailable").

### 7.3 S3: per-row source of actual points

- `espn_player_weekly_points.actual_source` (additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, no schema version
  bump): `provider` for Sleeper's `players_points`, `box_score` for Fantasy Intel's box-score scoring, and NULL when
  `actual_points` is NULL or the writer does not record it (the ESPN sync writes only ESPN's `appliedTotal`).
- Readers: the player route (`GET /api/players/{id}`, `routes.league_points_sources`) gives each `league_points` row its
  `actual_source` only when the player's rows are not all the provider's, so every ESPN league and every Sleeper player
  scored only by Sleeper keep their exact shape. The player comparison names the weeks scored from the box score in its
  notes. Older league files without the column read as before (`table_columns` check;
  `test_an_older_league_file_without_actual_source_reads_as_before`).
- Not yet surfaced: the waiver fallback percentile (`waivers.load_values`, used only for a player without usage data,
  who rarely has a box score), the player usage table (`player_week_usage.league_points`), and the frontend.

### 7.4 Unsupported rule ids through the D-12 contract

`league_rules.engine_support` adds `unsupported_rules` to an **unsupported** `skill_projections`, `kicker_projections`
or `dst_projections` row only (supported rows keep their shape): ESPN statIds as integers, a Sleeper league's own keys
in place of the synthetic ids. `league_compatibility` copies that list into the matching finding's `value`
(`{"unsupported_rules": [...]}`) and derives nothing itself. Each family now names only its own Sleeper keys
(`sleeper.scoring.unsupported_family`, the routing `to_scoring_items` applies): before, every unsupported Sleeper key
was named in every abstaining family's detail, so a kicker row could cite an IDP key. A kicker still abstains on a skill
key, because the translated item carries base points a K slot would earn; both rows then cite it, which is what the
resolvers do. Tests: `test_unsupported_rule_ids_reach_the_diagnostic_from_the_league_rules_per_family`,
`test_each_family_names_only_its_own_sleeper_keys`.

### 7.5 Cache and fingerprint audit

| Cache or key | Keyed by | Verdict |
|---|---|---|
| Response cache (`league_scope._RESPONSES`, `_response_key`) | `LeagueScope.cache_key` (league id, data and global versions, visibility epoch, perspective) + path + query | Scoring is part of the league file, so a scoring change is a new `data_version`. The provider wording applied before caching is a property of the league id. **Isolated.** Test: `test_the_response_cache_key_covers_league_versions_and_perspective`. |
| Derived runs (`rescore.apply_reference`) | the league's own resolved fingerprints | Rebuilt from the global references on every league build (all derived tables deleted first). A copy needs fingerprint equality; a re-score writes the league's fingerprint and a new run id. **Isolated** (`test_rescore.py`). |
| Projection ranges (`rescore` `RANGES`, `projection_ranges`) | copied only when the league's skill fingerprint equals the reference's; served only when the artifact's fingerprint equals the run's | **Isolated** (`test_rescore.py::test_projection_ranges_follow_only_the_reference_scoring...`). |
| Run selection (`projection_source.select_run`, kicker/D/ST stores) | the league's current fingerprint | A run made for another scoring in the file is never served. Test: `test_a_league_file_never_serves_a_run_made_for_another_scoring`. |
| Fingerprints | skill: effective weights + position weights; kicker: the per-kick points table; D/ST: the D/ST weights | Every scored rule changes its family's fingerprint (`test_every_scored_rule_changes_its_familys_fingerprint`). Unmodeled skill rules (defensive-return events) are not hashed; they change no projection. |
| `kicker_model._PTS_CACHE` | kicker scoring fingerprint | Values depend only on the scoring. **Isolated** (`test_the_kicker_points_cache_is_keyed_by_the_scoring_itself`). |
| Trade discovery `_CACHE`, `_VIEWS`, `_NEED_CACHE` | data version, league id, season, team and the search inputs | **Isolated** (read only; the file is owned by the trade-discovery budget work). |
| Action center summary cache | user, league, versions, visibility epoch, team, provider | **Isolated.** |
| Others (`provider_copy._CLASSIC`, `deps`, `jwt_auth`, `pg`, `providers/registry`, `kicker_venues`) | path and mtime, process, issuer, DSN, provider settings, static file | No league scoring or values. |

Every module-level mutable cache in `app/` is now listed with its reason in
`test_every_module_level_cache_in_the_app_is_reviewed_for_league_isolation`; a new one fails until it is reviewed.

### 7.6 Remaining after phase 2

1. Trade evaluation (`trades.py`) and trade alternatives (`trade_alternatives.py`) still call `source_label` without
   the state's values, so a Sleeper league without any Ours number is labelled "Ours" there. A one-argument change at
   each call, left out because the trade-discovery budget branch is editing both files. The frontend's
   `projectionName` maps any label other than Ours to "ESPN", so the trade pages also need "Unavailable" handled.
2. S3 readers not yet surfaced: waivers fallback percentile, `player_week_usage.league_points`, the frontend player
   page. The ESPN writer leaves `actual_source` NULL (provider points by construction).
3. S2 basis: waiver values in league points instead of full-PPR percentiles (the warning is done, #260).
4. Re-score accuracy evidence: a per-target-scoring backtest of the ratio re-score (section 6, item 2).
5. Additive shift clamp and forced negative starts (section 7.1): modelling decisions, not missing-as-zero.
6. Start/Sit's `projection_source_detail.counts.espn` counts every ESPN-origin player, including those without a number.
7. ESPN leagues name "ESPN" over a week with no ESPN number in the team_lineups tools (D-14 item 2); changing it moves
   ESPN goldens, so it is left for a reviewed golden update.

### 7.7 What phase 2 treated as D1-D3 and L2

The original review notes behind STATUS's "L2" and "D1-D3" were not recorded. Phase 2 used these definitions:

- **L2**: under `source=independent`, numbers from the provider fallback (where the models abstain) are labelled as
  provider numbers, never "Ours", per row and per response, including the "Ours" label of the team_lineups tools when
  no Ours number exists (section 7.2).
- **D1**: this document described PR #256 as open and the S1 label as not yet `partial` (section 2.4, section 5).
- **D2**: this document described the S2 waiver warning as firing only when `reception_points != 1` (section 2.4,
  section 6 item 4); #260 widened it.
- **D3**: stale anchors: the header's base commit and "this PR" wording, and line references into code changed since
  (`league_rules.py:737-742`, `:752-761`, `team_lineups.py:48-51`), now function names.

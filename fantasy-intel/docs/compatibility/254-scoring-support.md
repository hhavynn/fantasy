# #254 phase 1: actual-scoring support table

This table covers which fantasy-point numbers are **truly in a league's own scoring**, where each number comes from,
and which analysis tools read it. It covers QB, RB, WR, TE, K and D/ST, for ESPN and Sleeper, per rule category.
It meets #254 acceptance item 1. Decision D-12 (`docs/roadmap-execution/DECISIONS.md`) sets the ownership: #253 owns
diagnostics and the contract, and #254 owns scoring fidelity.

Paths are relative to `fantasy-intel/backend/`. Line numbers are at `origin/main` `e11ff5d`, plus this PR's
`app/analytics/rescore.py`. Each claim is marked as follows:

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
- **Sleeper free-agent "actual" points** are a Fantasy Intel box-score rescore stored in the provider column. See S3.

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
| **TE premium as a Sleeper position bonus** (`bonus_rec_te`, RB and WR likewise) | n/a | league-scored | **handled** by re-score (`positionPoints`, `league_scoring.py:157-163`, `sleeper/scoring.py:93-94`). **test** (`test_sleeper_scoring.py::test_a_te_premium_rescores_tight_ends_and_leaves_receivers_alone`). xFP and ratings ignore it, and `league_rules` says `partial` (`league_rules.py:752-761`). | as above | as above; waivers value is full PPR (S2) |
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
| waivers player value (S2) | xFP or usage percentile (full PPR), falling back to the recent actual-points percentile | **partial**. A warning appears only when `reception_points != 1`, so a TE-premium or 6-point-pass-TD league gets none. This is owned by the waivers agent; out of scope here. | `waivers.py:37-42`, `:430-463`; `waiver_data.py:170-171` |
| `league_rules` "Ours" engine row (S1 label) | says "scored with this league's rules" (`handled`) even for a ratio re-score | **partial**. PR #256 (#253 phase 2, open) adds `skill_runs_rescored` and marks it `partial`. Not touched here. | `league_rules.py:737-742` |
| team_lineups `source_label` | "Ours" for a Sleeper league even when no Ours run exists (S8 outside Start/Sit) | **partial**; listed in #253 section 8.3 | `team_lineups.py:48-51` |

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

## 4. S3: Sleeper `actual_points` provenance (documented, not fixed)

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
  backtest per target scoring exists. This is the reason S1 is labelled `partial` (PR #256).
- Cross-league fingerprint and cache isolation (`scoring_fingerprint`, `LeagueScope.cache_key`): not re-audited in
  phase 1, so **UNVERIFIED** (#254 acceptance item 6).

---

## 6. Recommended phase 2

1. **S3 provenance:** add `actual_source` (`provider` or `box_score`) to `espn_player_weekly_points` via `ADD COLUMN IF
   NOT EXISTS`. Set it in `sleeper/write.py`, and have the waivers fallback and player_compare surface or filter it.
2. **Re-score accuracy evidence:** a per-target-scoring backtest of the ratio re-score (half PPR, 6-point pass TD,
   first downs) against box-score outcomes. Until it exists, keep the `partial` label from #256.
3. **Additive shift path:** decide whether the `max(..., 0)` clamp (`rescore.py:140-144`) should allow negative
   projections, consistent with the ratio path.
4. **S2 basis:** with the waivers owner, warn on every non-PPR-equivalent rule (6-point pass TD, TE premium, first
   downs), not only `reception_points != 1`.
5. **Fixtures for acceptance items 2 and 3 across tools:** multiple FLEX slots, and a TE slot-override or kicker
   per-yard league through start_sit, trades and the waiver planner. Assert abstention or provider labelling end to
   end, reusing #253's `compat_fixtures.py` once it merges.
6. **Cache and fingerprint isolation audit** (acceptance item 6).

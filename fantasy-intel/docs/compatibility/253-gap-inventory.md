# #253 phase 1: league-format compatibility gap inventory

Read-only audit of `origin/main` at `ea9e664` (after #252), plus the #178 add/drop branch
(`origin/feature/fi-178-add-drop-sim` at `fe45d45`, read with `git show` only). No engine, API or frontend code was changed.
Decision D-12 (`docs/roadmap-execution/DECISIONS.md`) sets the rules this report follows:

- It reuses the `handled / partial / unsupported / unverified` vocabulary of `analytics/league_rules.py`.
- It keeps four dimensions apart: import readiness, roster legality, scoring and projection fidelity, and feature availability.
- #253 owns structure, fixtures and diagnostics. #254 owns scoring fidelity.
- Roster support is not scoring fidelity.

All data is synthetic. No real league, credential, provider endpoint or Fantasy MCP tool was used.

**How claims were checked.** A claim marked **verified (probe)** was run against the real parsers and engines. The runs
were pure function calls on invented inputs, in a scratch script outside the repository. A claim marked **verified
(test)** is pinned by an existing test that passed at this head. A claim marked **from code** was read from the source
and not executed. A claim marked **UNVERIFIED** is an inference that still needs a fixture test.

Paths are relative to `fantasy-intel/backend/` unless they start with `fantasy-intel/`.

**Targeted tests run (all pass):** `test_league_rules.py`, `test_lineup.py`, `test_lineup_flex_solver.py`,
`test_team_lineups.py`, `test_trade_assets_superflex.py`, `test_sleeper_taxi.py` and `test_bye_planner.py`, 114 tests in
total. The one Postgres test needed `FI_TEST_PG_DSN` on the shared cluster. No `agent_check.py affected`, `security`,
Playwright or full suite was run.

---

## 1. Import pipeline, traced from the code

The provider adapters in `app/leagues/providers/*.py`, behind the `LeagueProvider` interface, are **not** on the data
path. Only `GET /v2/providers` uses them, through `registry.all_capabilities()` in `app/api/leagues_v2.py:124-129`. Their
`lineup_rules()` and `scoring_rules()` normalizations (`providers/espn.py:82-140`, `providers/sleeper.py:98`) are tested
but never written to a league file. A compatibility diagnostic must therefore read the league file, not the adapter
layer.

### 1.1 ESPN

| Stage | Code | Roster slots / counts | Bench / IR / taxi | Scoring |
|---|---|---|---|---|
| fetch | `app/sync/espn_source.py:77` `read_league` -> `EspnPayloads` | raw `settings.rosterSettings.lineupSlotCounts` (slot id -> count) | ESPN has no taxi | raw `settings.scoringSettings.scoringItems` |
| build | `app/sync/build.py:74-115` `build_league_file`: copy of the global file + previous league file + `write_synced` + `apply_reference` (rescore) + `run_analytics` -> `split_database` | | | |
| normalize | `app/espn/parse.py:67-115` `parse_league` | `slot_counts = {SLOT_MAP.get(int(id), str(id)): count ... if count}` (`parse.py:79-83`). Zero counts are dropped. Unknown ids keep their number as the label. `SLOT_MAP` is in `app/espn/constants.py:4-9`; id 22 maps to the label `""`. | BE = id 20, IR = id 21; `NON_STARTER_SLOTS = {20, 21, 22}` (`constants.py:10-12`) | `scoring_items` stored verbatim (`parse.py:111`); `reception_points` = the last statId 53 item (`parse.py:84-87`); `settings_raw` = settings with member ids scrubbed |
| rosters | `parse.py:229-271` `parse_rosters` -> `lineup_snapshots` | `lineup_slot_id`, `lineup_slot = SLOT_MAP.get(id)`, `is_starter = id not in NON_STARTER_SLOTS` | IR rows keep id 21 | provider `points` / `projected_points` (league-scored by ESPN) |
| players | `parse.py:200-225` `player_row` | `position = DEFAULT_POSITION_MAP[defaultPositionId]` (one position; `eligible_slots` is stored but no engine reads it) | | |
| store | `app/espn/sync.py:157-184` `write_all`; `leagues` table in `app/db/schema.sql:30-51` | `leagues.roster_slots` JSON | | `leagues.scoring_items`, `espn_player_weekly_points.{actual,projected}_points` (`schema.sql:122-133`) |
| API | `app/api/deps.py:113-131` `db()`: league-scoped layered cursor under `/v2/leagues/{lg}/api/...` (`app/api/league_scope.py`, `app/main.py:109-115`); league row via `analytics/legacy_league.py:19` `latest_league` (one row per league file) | | | |
| Next.js | `fantasy-intel/frontend/lib/api.ts:70` `api()` -> `scopedPath` -> `lib/league-scope.ts:63-66` `scopeApiPath` (`/v2/leagues/{lg}/api/...`); rules page `app/league-rules/page.tsx:22`, types `lib/league-rules.ts` | | | |

**FX-17 through ESPN, verified (probe).** The input is `lineupSlotCounts` = `{0:2, 2:3, 4:4, 6:2, 7:1, 23:3, 17:1, 16:1,
20:10, 21:3}` plus the zero ids 1, 3, 5, 22 and 24.

- `roster_slots` comes out as `{QB:2, RB:3, WR:4, TE:2, OP:1, FLEX:3, K:1, D/ST:1, BE:10, IR:3}`, unchanged by normalization.
- `lineup.lineup_slots` gives 17 slots, in this order: 2 QB, 3 RB, 4 WR, 2 TE, K, D/ST, 3 FLEX, OP.
- `start_sit.build_slot_instances` gives 17 instances (`QB_0` to `OP_0`).
- `reserve_slots.active_capacity` = 27. `waiver_roster.roster_constraints`: capacity 27, `settings_complete` True.
- `bye_planner.starting_slots` reports no unresolved slots.
- `league_rules`: starters 17, bench 10, IR 3, `flex_types ["FLEX","OP"]`, `superflex` True.

**What could change or drop slot counts (ESPN).**

- (a) A zero count is dropped. This is harmless.
- (b) An unknown id keeps its numeric label. `league_rules` lists it as unmapped. `lineup_slots` and `build_slot_instances`
  silently skip it, and so do every lineup total (see G3).
- (c) Slot 22 has the label `""`. `parse_rosters` treats it as a non-starter, and so does the adapter
  (`providers/espn.py:55`). But `league_rules._starters` (`league_rules.py:207-212`) counts any non-zero `""` slot as an
  "unrecognised" starter. Whether ESPN ever sends a non-zero count for id 22 is UNVERIFIED.
- (d) A dual-eligible player can fill only his `defaultPositionId` slot (G11).
- No path rescales or caps the FX-17 counts. The solvers refuse anything above `MAX_FLEX_SLOTS = 30` (`lineup.py:27`), and
  the bye planner evaluates at most `MAX_STARTING_SLOTS = 30` starting slots (`bye_planner.py:63`).

### 1.2 Sleeper

| Stage | Code | Roster slots / counts | Bench / IR / taxi | Scoring |
|---|---|---|---|---|
| fetch | `app/sync/sleeper_source.py:52` `fetch` -> `SleeperPayloads` | `league.roster_positions` (list) | `settings.reserve_slots`, `settings.taxi_slots`; rosters `reserve[]`, `taxi[]` | `league.scoring_settings` (key -> points) |
| normalize | `app/sleeper/normalize.py:32-46` | `Counter(SLOT_MAP.get(p, p) for p in roster_positions)`; `SLOT_MAP` in `app/sleeper/constants.py:4-12` (`SUPER_FLEX` to `OP`, `WRRB_FLEX` to `RB/WR`, `REC_FLEX` to `WR/TE`, `IDP_FLEX` to `DP`, `DEF` to `D/ST`, `BN` to `BE`). Unknown entries keep their label. | `IR = reserve_slots`, `TAXI = taxi_slots`, both overwritten from settings and not counted in `roster_positions` | `league_row` stores the raw dict; `write.py` replaces it with translated items |
| write | `app/sleeper/write.py:166-240` `write_sleeper` | `leagues.roster_slots` | lineup rows (`write.py:250-319`): starters by index into `starting_slots`; current week IR to label IR / id 21, taxi to label TAXI / **id 20** (`write.py:39-41`, documented in `reserve_slots.py`) | `scoring_items = to_scoring_items(...)` (`app/sleeper/scoring.py:73-121`); `settings_raw` keeps the verbatim `scoring_settings`, `scoring_unsupported` and `scoring_notes` (`write.py:186-192`) |
| points | `write.py:13-15`, `250-266`, `365-386` | | | rostered player-weeks use Sleeper's own `players_points` (provider); free-agent player-weeks use an nflverse box score scored by Fantasy Intel with the league's skill rules (`_box_score_points`, skipped when unsupported); `projected_points` stays NULL |
| API / Next.js | as for ESPN; `projection_source.default_source` (`analytics/projection_source.py:99-110`) defaults a Sleeper league to `independent` (Ours); `app/api/provider_copy.py` rewrites ESPN wording | | | |

**Sleeper analog of FX-17, verified (probe).** The input is `roster_positions` = 2 QB, 3 RB, 4 WR, 2 TE, `SUPER_FLEX`,
3 `FLEX`, K, DEF and 10 BN, with `reserve_slots` 3 and `taxi_slots` 0.

- `roster_slots` matches ESPN exactly: `{QB:2, RB:3, WR:4, TE:2, OP:1, FLEX:3, K:1, D/ST:1, BE:10, IR:3}`.
- `starting_slots` keeps Sleeper's order, aligned with `roster.starters`.
- `roster_constraints`: capacity 27 but **`settings_complete` False**, so waiver add/drop legality is always UNKNOWN (G6).

**What could change or drop counts or players (Sleeper).**

- (a) The `IR` and `TAXI` counts come only from settings. This is correct because Sleeper does not list them in
  `roster_positions`.
- (b) IDP slots (`DL`, `LB`, `DB`, `DP`) are counted. But `_players` keeps only QB, RB, WR, TE, K and D/ST
  (`write.py:340-345`), and `_lineup` stores any slot outside `SLOT_IDS` with the bench id 20 (`write.py:321-326`).
  **Rostered IDP players therefore vanish from every roster query that joins `fantasy_players`** (G4, from code).
- (c) Played weeks cannot tell a taxi player from a bench player (documented in `reserve_slots.py`).
- (d) Positions come from `pl.position`. Sleeper's multi-position `fantasy_positions` is not used. Impact is UNVERIFIED.

---

## 2. Per-tool matrix

Status words follow `league_rules`. "FX-17 structure" means multi-QB, OP, 3 FLEX, 17 starters, 10 BE and 3 IR. "Source"
means provider points (ESPN projected or actual, Sleeper actual), Ours (the independent skill model, possibly re-scored),
or the kfp / dfp K and D/ST models.

| Tool | Reads | FX-17 structure | Scoring source | Unsupported or missing data | Existing tests | Gaps |
|---|---|---|---|---|---|---|
| **league_rules (#169)** `analytics/league_rules.py` | `leagues` row only | handled: verified (probe), see 1.1 | describes rules; `engine_support` restates the resolvers (`league_rules.py:696-788`) | missing value: `None` + reason (`fact()`, `:71-77`); unknown slot or rule listed under `unmapped` | `test_league_rules.py` (unusual ESPN and Sleeper fixtures with OP, RB/WR, WR/TE, IDP, ER, TE slot override, unmapped stat) | `engine_support` rows have no stable code: `lineup_what_if.py:360` matches the English string `"Lineup lock"` (G14). It does not state import readiness, Sleeper's "no provider projections" boundary, or which tools a missing family blocks (G10). The Ours row says "scored with this league's rules" (`:738`) even for a ratio re-score (S1). Slot `""` counting is inconsistent (1.1c). |
| **lineup / `assign_lineup` (#252)** `analytics/lineup.py` | slot list + `{id, position, value}` | handled: exact solver for mixed flex; FX-17 shape timed in `test_lineup.py:136-144` | none (pure) | `value` None becomes **0.0** (`lineup.py:56, 140-141, 268-269`); callers must filter None first. An empty slot gets value 0.0. Labels outside FIXED+FLEX are silently skipped by `lineup_slots` (`:39-46`). Too many slots raise `LineupTooLargeError`, answered with 422. | `test_lineup.py`, `test_lineup_flex_solver.py` (exhaustive parity, ties, starved pools, 30-slot cap) | No error or flag for a skipped slot label (G3) |
| **start_sit** `analytics/start_sit.py` | `lineup_snapshots` + `fantasy_players` (`:325-361`), `espn_player_weekly_points`, `projection_source.weekly` (`:798-805`) | structure handled (`build_slot_instances` `:221-235`, verified (probe)); no OP/multi-QB test | ESPN provider points by default; Ours opt-in (or the Sleeper default) | Players with None are excluded from candidates. **An empty recommended slot does not make the total incomplete** (`:489-507`): a K slot with only an unprojected K shows `recommended_total` as complete without the K, and with every projection missing a preview shows `recommended_total` 0.0 and `unconstrained_total` 0 with `is_complete` True, verified (probe) (**G1**). A starter in an unfilled slot (IDP) is counted in `actual_total` but no slot exists for him in the recommendation, so `projected_gain` is negative, verified (probe) (**G2**). A verified NFL-ineligible starter is set to 0.0 (`:457`, `:480`); this is documented and is a modeled zero. | `test_start_sit.py` (1QB/1FLEX only), `test_ir_semantics.py`, `test_sleeper_taxi.py` | G1, G2. `projection_source` says "ESPN" whenever Ours is unavailable (`:1116`), and `provider_copy` rewrites a bare "ESPN" to "Ours" (`app/api/provider_copy.py:56`), so a Sleeper league whose Ours abstained is labelled "Ours" with no numbers (S8). No superflex or 3-FLEX test. |
| **what-if (#171)** `analytics/lineup_what_if.py` | start_sit `capture` | handled (slot regex covers every FIXED and FLEX label, index of up to 2 digits) | inherits start_sit | its own `totals()` keeps missing as unknown (`lineup_what_if.py:194-208`); the baseline inherits G1 | `test_lineup_what_if.py` (1QB) | depends on the rule name (G14); no FX-17 test |
| **team_lineups / lineup projections** `analytics/team_lineups.py` | `LeagueState` | handled | `trade_data.load_league_state`: ESPN or Ours (`trade_data.py:243-254`) | correct "missing is not zero" rule (`:208-257`): a lineup-relevant, teamed, unprojected player makes the week incomplete, verified (probe). **Unfilled slot labels (IDP, P, HC) leave the total with `projection_complete` True**, verified (probe) (G3). | `test_team_lineups.py` (QB+WR+OP), `test_ir_semantics.py`, `test_sleeper_taxi.py` | G3; one missing family (for example Sleeper K) makes every team-week incomplete (G10) |
| **team_value** `analytics/team_value.py` | `load_race_data` (playoff_race) -> `load_league_state` | inherits | ESPN or Ours | inherits team_lineups; ranks only on comparable weeks | `test_team_value.py` (1QB synthetic) | G3, G4, G10; IDP player marginal value 0 instead of abstaining (from code) |
| **waivers** `analytics/waivers.py` | `player_scores` percentiles, `lineup_snapshots`, `load_waiver_context` | structure handled (`lineup_slots`; OP counted in needs `:126-168`) | **player value = xFP / usage percentile (full PPR, not league scoring)** with ESPN or provider league-points percentile as fallback (`:430-463`); forward effects use ESPN or Ours projections | **a missing value enters the lineup as 0.0** (`team_state` `:89`, candidate `:262`), verified (probe); an empty dedicated slot is padded with 0.0 (`:102-107`). OP and FLEX choices compare position-relative percentiles across positions. **Fixed in #260:** a missing value is unknown; gains, drop costs, needs and the ranking that depend on it are partial or ranked last, per slot group (an empty slot stays a measured 0); OP/FLEX percentile comparison is labelled, not fixed. | `test_waivers.py`, `test_waiver_data.py`, `test_waiver_decisions.py` (no OP or multi-QB) | G5 (structure: partial); S2 (scoring) |
| **waiver_decisions / waiver_roster** | `roster_constraints` (`waiver_roster.py:17-33`), `plan_roster_move` (`:75-...`), `forward_effect` (`:53-72`) | ESPN: capacity 27, verified (probe). Any slot outside FIXED+FLEX+BE/IR/TAXI makes capacity None, so the status is UNKNOWN (good abstention). | as waivers | `forward_effect` skips weeks with a missing projection (`missing_weeks`; total None). **Sleeper: `settings_complete` = `bool(rosterSettings)` is False, so every plan is UNKNOWN** (`:31`, `:91`). This is pinned around in `test_sleeper_taxi.py:171-173`. IR-eligible statuses are ESPN's (`:88`). | `test_waiver_decisions.py`, `test_sleeper_taxi.py` | G6, G7 |
| **add/drop (#178, branch)** `analytics/add_drop.py` @ `fe45d45` | `load_league_state`, `trades.side_lineups` / `legal_roster` / `resolve_roster` | inherits trades (structure handled) | ESPN or Ours (`source_label`) | week delta None unless both lineups are complete; IDP add flagged `NOT_LINEUP_ELIGIBLE`; IR eligibility via the new `ir_slot_eligible` (same ESPN statuses) | `test_add_drop.py` (1QB/1FLEX `SLOTS`, IR, taxi, limits) | G3, G4 (capacity counts IDP slots while IDP players are dropped from rosters), G7; no OP or 3-FLEX test |
| **trades** `analytics/trades.py`, `trade_data.py` | `load_league_state` | handled (mixed flex uses the exact solver) | ESPN or Ours | completeness per week; `legal_roster` uses `active_capacity` (all slots except IR and TAXI, `trades.py:135-141`, `trade_data.py:267`) | `test_trades.py`, `test_trade_data.py`, `test_trade_v3.py` | G3, G4 |
| **trade_discovery** `analytics/trade_discovery.py` | `LeagueState` | handled: FX-17 has two flex types, so `general` uses `assign_lineup` (`:95-130`) | ESPN or Ours | `_Totals.missing` keeps completeness; `value()` returns 0.0 for an absent row only inside the bounds (`:173-176`) | `test_trade_discovery.py` (1QB synthetic) | no superflex or 3-FLEX parity test of bounds against the exact search |
| **trade_assets (#229 OP-aware)** `analytics/trade_assets.py` | `project_team` reference lineups | handled: OP counted for its occupant; 2-OP and 3-FLEX tests | ESPN or Ours (PAR) | PAR None when a relevant week is missing; **a sparse pool gives replacement level 0.0** (`:175`), and `.get("level", 0.0)` (`:212`, `:226`) inflates PAR (G8, from code) | `test_trade_assets_superflex.py` (brute force, OP, 2-OP, three-flex, deterministic) | G8 |
| **bye_planner (#177)** `analytics/bye_planner.py` | slots, snapshots, schedule | handled; slots it cannot resolve make the week `unconfirmed` (`:302-325`) | none (no projections) | unknown never counted; IR and taxi never cover | `test_bye_planner.py` (FLEX 8 + OP 2 at `:324`) | none structural; IDP slots give `unconfirmed`, which is correct |
| **kdst_streaming (#181)** `analytics/kdst_streaming.py` | `load_league_state`, schedule, kfp/dfp runs (shown beside the plan, never its input) | K and D/ST count 1 handled; **2+ K or D/ST slots are planned as one slot** (`:23`, `:84`, `:541`) | ESPN or Ours projections | an unknown week is never 0; the plan stops at the data horizon | `test_kdst_streaming.py`, `test_kdst_streaming_planner.py` | G9 |
| **action_center (#166)** `app/api/action_center.py` | runs `/api/overview`, `/api/start-sit` and `/api/league-rules` per authorized league (`:220-255`) | inherits | inherits start_sit | per-league card with `not_ready` / `needs_team` states | `test_action_center.py` | inherits G1 and G2; no compatibility summary per league |
| **rankings** `analytics/rankings.py` | QB-TE pool, ESPN or Ours projections | not slot-based (positional ranks) | ESPN or Ours (Ours opt-in) | a total is NULL unless every week is available; `projection.status` explains | `test_rankings.py` | none for structure; S1 applies when Ours is re-scored |
| **power_rankings** `analytics/power_rankings.py` | team_value ranks, `project_team`, all-play | inherits | ESPN or Ours | inherits; K and D/ST excluded from depth and replacement | `test_power_rankings.py` | G3, G10 |
| **playoff race** `analytics/playoff_race.py` | provider results + `project_team` | inherits | ESPN or Ours (forward); provider actuals | an incomplete side leaves the matchup unresolved | `test_playoff_race.py` (`synthetic_race` QB/RB/FLEX) | G3: projected winners omit IDP slots on both sides |

Not audited: `matchup.py`, `manager_loader.py` (reads `roster_slots` at `:156`), `start_sit_evaluation.py` (research)
and `overview`. They are UNVERIFIED for FX-17.

---

## 3. Synthetic fixtures today

| Fixture | Where | Formats covered |
|---|---|---|
| ESPN fixture league | `tests/espn_fixture.py` (`league_payload`, `roster_payload`, `FakeEspnClient`), used by `scripts/build_e2e_db.py` and goldens `tests/golden/e2e/*.json` | 4 teams; 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, K, D/ST, 3 BE, 1 IR; scoring is **only** `statId 53 = 1.0`, so the skill resolver supports it with receptions-only weights, and kicker and D/ST are unsupported (verified (probe)) |
| Sleeper e2e league | `scripts/build_e2e_sleeper.py` | 4 teams; `QB RB RB WR WR TE FLEX SUPER_FLEX K DEF` + 5 BN, `reserve_slots` 1; half PPR, TE premium 0.5, Sleeper FG buckets and PA tiers; sealed Ours, K and D/ST runs |
| Sleeper recorded-shape JSON | `tests/fixtures/sleeper/*.json`, `app/leagues/providers/fixture_data/sleeper/` | 1 QB, superflex, `reserve_slots` 1, `taxi_slots` 1 |
| Unusual rules | `tests/test_league_rules.py:36-72` (`unusual_espn`, `unusual_sleeper_league`) | OP, RB/WR, WR/TE, FLEX, LB, ER(24), TE slot override, unmapped stat 8888; Sleeper DL, IDP_FLEX, idp_tkl, bonus_pass_yd_300, unknown key. These are exercised **only** by league_rules, not by consumer engines. |
| Multi-league E2E | `scripts/e2e_multileague.py` | publishes ESPN public, ESPN private and Sleeper leagues (the two above) plus stale and blank sync states |
| Pure `LeagueState` builders | `tests/synthetic_league.py` (`slots=` and `roster=` parameters), `synthetic_v3.py`, `synthetic_race.py`, `synthetic_value.py`, `synthetic_forecast.py`, `synthetic_kicker.py` | 1QB/1FLEX shapes (`synthetic_race`: QB/RB/FLEX) |
| Superflex-specific | `tests/test_trade_assets_superflex.py`, `test_lineup*.py`, `test_bye_planner.py:324` | OP, 2 OP, 3 FLEX, 8 FLEX; pure engine inputs only |

**What is missing.** No fixture goes from import to analysis with multi-QB, OP, 3 FLEX, 17 starters, 10 BE and 3 IR, and
none has six teams. No consumer engine other than league_rules is exercised with IDP, P, HC or ER slots, a 2-K league,
Sleeper taxi plus superflex, or unmapped custom scoring.

### 3.1 Proposed reusable fixture matrix

| Id | Provider(s) | Structure | Scoring | Edge cases | What it pins |
|---|---|---|---|---|---|
| M1 std-redraft | ESPN | current fixture shape (1 QB, 2 RB, 2 WR, TE, FLEX, K, D/ST) | full PPR, known items | baseline | goldens unchanged |
| M2 FX-17 | ESPN | 6 teams; 2 QB, 3 RB, 4 WR, 2 TE, 1 OP, 3 FLEX, K, D/ST, 10 BE, 3 IR | (a) known standard items (full PPR); (b) the same plus an unmapped stat and a TE slot override, so Ours is unsupported while ESPN projections stay | ties, a full IR, an OP filled by QB vs RB | normalization unchanged; 17 distinct starters; no BE or IR start; 3 interchangeable FLEX |
| M3 FX-17 Sleeper | Sleeper | the same via `roster_positions` + `reserve_slots` 3 | half PPR, known | Ours-only projections | the same invariants; Sleeper default source |
| M4 superflex multi-flex sparse | both | QB, OP, RB/WR, WR/TE, FLEX x2 | full PPR | tied projections; fewer eligible players than slots; no free-agent QB projected | solver ties; G1, G8 |
| M5 PPR variants | both | M1 | 0.0 / 0.5 / 1.0 receptions | | `format`, `points_per_reception`; waiver PPR warning |
| M6 custom known / unmapped | both | M1 | known: 6-point pass TD, first downs (Sleeper); unmapped: `bonus_pass_yd_300`, `zz_new_rule`, ESPN 8888, `fum` | | Ours or K abstain; G10 |
| M7 IR + taxi | Sleeper (`type` 2) | M3 + `taxi_slots` 3 | half PPR | taxi player on a full roster; IR occupant with a healthy status | capacity; G6, G7 |
| M8 IDP / atypical | both | ESPN LB, DL, P, HC, ER; Sleeper DL, LB, DB, IDP_FLEX | idp_* keys | rostered IDP starters | G2, G3, G4; `unsupported` labels |
| M9 2K / 2DST | ESPN | K x2, D/ST x2 | known | | G9 |

### 3.2 Where FX-17 should live and how tests use it

- **`fantasy-intel/backend/tests/compat_fixtures.py`** (new, pure, no database), built on the existing helpers:
  - `espn_payload(slot_counts, *, size, scoring_items, rosters)` wraps `espn_fixture.league_payload()` and
    `roster_payload(period, rosters)` and overrides `settings`, exactly as `test_league_rules.unusual_espn` already does.
    Invented player ids sit in a reserved range (for example 70_000+), with names like "FX QB 01". That covers 6 teams x
    (27 active + 3 IR) players, which the 32-player `espn_fixture.PLAYERS` cannot.
  - `sleeper_league(roster_positions, *, reserve_slots, taxi_slots, scoring_settings)` loads
    `tests/fixtures/sleeper/league.json`, as `test_league_rules.unusual_sleeper_league` already does.
  - `fx17_state(...)` calls `synthetic_league(n_teams=6, slots=lineup_slots(FX17), roster=..., ir_per_team=3, capacity=27)`
    for pure engine tests.
  - `MATRIX` is a list of `(id, provider, builder)` rows for `pytest.mark.parametrize`.
- **`tests/test_compat_matrix.py`** (new), in two tiers:
  1. *Import tier* (tmp DuckDB, no Postgres). For ESPN, call `parse_league` and then
     `write_all` through a `FakeEspnClient` subclass that returns the FX-17 payload. For Sleeper, call `write_sleeper`
     with `SleeperPayloads`, the way `test_league_rules.sleeper_con` does. Assert that `roster_slots` matches the
     input, then `build_league_rules`, then the compatibility diagnostic.
  2. *Engine tier* (pure `LeagueState` plus the import-tier file). Run start_sit, team_lineups, trades and discovery,
     waivers, bye_planner and kdst. Assert the invariants (17 unique starters, no BE, IR or taxi starter, three FLEX
     filled, totals) and each tool's diagnostic status.
- Leave `espn_fixture` defaults, `build_e2e_db.py` and the goldens untouched. A fourth E2E league in
  `scripts/e2e_multileague.py` is a later UI step, not phase 2.

---

## 4. Draft diagnostic contract (shape only)

`league-compatibility/v1`, computed per request from the league file (no writes, no provider calls). It **extends**
`league-rules/v1`.

**Additive change to league_rules.** Each `engine_support` row gains a stable `code`, for example `superflex_slot`,
`flex_types`, `unfilled_slots`, `taxi`, `skill_projections`, `kicker_projections`, `dst_projections`, `te_premium`,
`ppr`, `lineup_lock`, `position_limits` and `undroppable`. Existing fields stay unchanged, so the frontend
`EngineRule` type and `lineup_what_if` keep working; what-if should switch to `code`. The diagnostic **refers** to those
codes and never re-derives scoring support. #254 later adds or refines `engine_support` rows, and its scoring reasons
flow through unchanged.

```text
{
  contract: "league-compatibility/v1",
  source: <league_rules.source>,                       # provider, captured_at, note (no ids, no credentials)
  dimensions: {                                        # league-wide, never collapsed into one "ready"
    import:  Entry,                                    # synced row, slots, scoring, current lineup snapshot, player coverage
    roster:  Entry,                                    # every slot fillable by an engine; capacity, limits, IR/taxi rules known
    scoring: Entry                                     # summarises engine_support codes skill/kicker/dst/te_premium/ppr
  },
  tools: [ {
    tool: "start_sit" | "what_if" | "lineup_projections" | "team_value" | "waivers" | "add_drop" | "trades"
          | "trade_discovery" | "bye_planner" | "kdst_streaming" | "rankings" | "power_rankings" | "playoff_race",
    label: str,
    import: Entry, roster: Entry, scoring: Entry, feature: Entry,   # the four D-12 dimensions, per tool
    projection_source: "provider" | "ours" | "ours_rescored" | "none" | null,
    refs: [engine_support code, ...]                   # which league_rules rows this rests on
  } ],
  unmapped_count: int                                  # pointer to league_rules.unmapped, not a copy
}
Entry = {
  status: "handled" | "partial" | "unsupported" | "unverified",
  reason: str,                    # human-readable, sentence case
  missing_input: str | null,      # e.g. "kicker projections in this league's scoring"
  provider_boundary: str | null,  # e.g. "Sleeper publishes no projections" (by capability name, from /v2/providers)
  action: str | null,             # what the manager can do (re-sync, check rules page, nothing)
  evidence: {code: str, value}[]  # small facts: counts, ids of codes, never player or member data
}
```

**Rules.** There is no overall boolean, and the UI shows the four columns. A dimension takes the worst of its inputs, in
the order `unsupported` > `unverified` > `partial` > `handled`. A tool whose total would omit a slot (G3) gets
`roster.partial`, not `handled`. A tool whose projections are absent because a family abstained gets
`scoring.unsupported` with `refs`, and `feature.unverified` if its totals become incomplete (G10). Example for FX-17 ESPN
(M2a): import handled, roster handled, scoring handled (provider projections, ESPN-scored), `feature` per tool, with
waivers `scoring.partial` (xFP is full PPR).

**Endpoints (league-scoped only).**

- `GET /v2/leagues/{league_id}/api/league-compatibility`, a new route in `app/api/league_rules.py` that
  `mount_legacy_routers` mounts automatically and that is authorized by `league_scope`. Mounted legacy routes are
  covered by `test_league_scope.py` (the per-league golden check), not by `test_v2_authorization_matrix.py`.
- Optionally the same block as `compatibility` inside the league-rules payload, so one fetch serves the rules page.
- The legacy unscoped `/api/league-compatibility` exists only in owner single-league mode, as `/api/league-rules` does
  today.
- **Never** in `/v2/providers` (global, provider-level only) and never in global caches. `/v2/action-center` may later
  carry per-league status counts only, inside each authorized league's section.

---

## 5. Prioritized gaps

"#253" means structure and diagnostics; "#254" means scoring fidelity. Each entry gives a minimal reproduction idea.

### Missing data becomes 0, or unsupported data is presented as league-scored

| # | Owner | Pri | Finding | Evidence | Minimal reproduction |
|---|---|---|---|---|---|
| **G1** | #253 (bug) | **P0** | Start/Sit treats an empty recommended slot as complete. If a slot can only be filled by an unprojected player, `recommended_total` excludes him and says complete. With every projection missing, a future preview shows `recommended_total` **0.0** and `unconstrained_total` **0** with `projection_complete` True, and the UI shows it as a complete total (`frontend/lib/start-sit-helpers.ts:68-77`). In a Sleeper league whose Ours abstains (unsupported skill scoring, no provider projections), every projection is None, and the source label is rewritten to "Ours" (S8). team_lineups applies the opposite, correct rule. | `start_sit.py:489-507`; verified (probe). The Sleeper API path is UNVERIFIED end to end. | `optimize_lock_constrained_lineup` with FX-17 slots, every projection None, `actual_starters=[]`, then assert `recommended_total is None`. Also: QB 20.0 + K None, assert incomplete. |
| **G5** | #253 (structure) / #254 (basis) | P1 | Waiver lineup math sets a missing value to 0.0 (`raw or 0.0`, `cv.value or 0.0`) and pads empty dedicated slots with 0. OP and FLEX picks compare position percentiles, not points. | `waivers.py:89`, `:102-107`, `:262`; verified (probe) | `team_state([QB without player_scores], {}, {"QB": 1})` gives lineup value 0.0. **Fixed in #260** (lineup value None, gain/need/drop cost partial per slot group, unknown benefit ranked last; the OP/FLEX percentile comparison is labelled, and a points basis is a follow-up). |
| **G8** | #253 | P2 | trade_assets replacement level is 0.0 when a position has fewer projected players than weekly starters and no projected free agent, so PAR equals the raw points. | `trade_assets.py:175`, `:212`, `:226`; from code | M4 sparse superflex: 6 teams, 12 QB starters, 11 projected rostered QBs, no free-agent QB, then assert level None or `insufficient_data` |
| **S1** | #254 | P1 | Ours for a non-reference scoring is a **ratio re-score** of reference-scoring projections (`rescore.py`, flag `RESCORED_FLAG`), but `engine_support` says "scored with this league's rules" (HANDLED). `stat_mix` uses `avg(coalesce(c, 0))`, so a missing box-score component counts as 0. | `league_rules.py:737-742`; `rescore.py:43`, `:66-80` | league with 6-point pass TD: `engine_support` Ours row is `handled` while the rows carry `RESCORED_FLAG` |
| **S2** | #254 | P1 | Waiver player values are xFP or usage percentiles (full PPR), not league scoring. A warning appears only when `reception_points != 1` (`waiver_data.py` "Historical player value and xFP use PPR-based usage"), so a 6-point pass TD or TE premium league gets no warning. The fallback basis is labelled "ESPN league-points" in Sleeper leagues too (rewritten by `provider_copy`). | `waivers.py:37-42`, `:430-463` | M6 known-custom ESPN league: waiver warnings list nothing. **Warning widened in #260:** it also fires, naming the difference, for pass-TD and other basis rules, TE premium, first downs/attempts, and counts other offensive rules; the value basis itself is still #254's. |
| **S3** | #254 | P2 | Sleeper `actual_points` mixes provider points (rostered weeks) with Fantasy Intel box-score rescores (free-agent weeks) in one column with no per-row provenance. Unmodeled events count as 0. | `write.py:13-15`, `:365-386`; `schema.sql:122-130` | M3: a player's actual points before and after his add use different sources, with no field saying so |
| **S8** | #253 (label) | P2 | start_sit returns "ESPN" when Ours is unavailable (`start_sit.py:1116`); `provider_copy` maps the bare "ESPN" to "Ours" (`provider_copy.py:56`). A Sleeper league whose Ours abstained is labelled "Ours" with no numbers (`projection_source_detail.available` is False). | from code | M6 Sleeper with `bonus_pass_yd_300`: `GET start-sit`, `projection_source == "Ours"`, `detail.available == False` |

### Structure and diagnostics (#253)

| # | Pri | Finding | Evidence | Minimal reproduction |
|---|---|---|---|---|
| **G2** | P1 (bug) | Start/Sit: a starter in a slot no engine fills (IDP, P, HC) is counted in `actual_total` but cannot appear in the recommendation, so `projected_gain` is understated (negative). | `start_sit.py:266-281`, `:474-487`; verified (probe): actual 38, recommended 30, gain -8 | slots `{QB, RB, LB}`, all projected, LB starter: assert gain 0 or the tool abstains |
| **G3** | P1 | Every full-lineup total (team_lineups, trades, discovery, team value, power rankings, playoff race, add/drop) silently omits slot labels outside FIXED+FLEX and reports `projection_complete` True. Only the league-rules page discloses it ("Starting slots no engine fills"). | `lineup.py:39-46`; `team_lineups.py:111-113`, `:208-245`; verified (probe) | `project_week` with slots from `{QB:1, LB:1}`: total 20, complete True |
| **G4** | P1 | Sleeper IDP: IDP players are never written to `fantasy_players` and IDP starters get bench id 20, so roster queries that join `fantasy_players` drop them. `active_capacity` still counts IDP slots, so trades and add/drop may see a free spot that does not exist. | `write.py:321-326`, `:340-345`; `trade_data.py:220-229`, `:267`; from code, UNVERIFIED end to end | M8 Sleeper: full roster with 1 rostered DL, receive 2-for-1, assert a forced drop (or a roster `unsupported` diagnostic) |
| **G6** | P2 | Sleeper waiver add/drop legality is always UNKNOWN (`settings_complete = bool(rosterSettings)`). This is a conservative abstention, but nothing on the rules page or the diagnostic says so. | `waiver_roster.py:31`, `:91`; pinned around in `test_sleeper_taxi.py:171-173` | M3: every waiver row's move status is `UNKNOWN` |
| **G7** | P2 | IR eligibility uses ESPN statuses (`IR`, `INJURY_RESERVE`, `OUT`) for Sleeper leagues; Sleeper's `reserve_allow_*` settings are not read. Which settings real leagues carry is UNVERIFIED. | `waiver_roster.py:88`; branch `ir_slot_eligible` | M7: an IR occupant who is `DOUBTFUL` in a league allowing doubtful players blocks adds |
| **G9** | P2 | kdst_streaming plans one slot per position even when the league starts 2 K or 2 D/ST. | `kdst_streaming.py:23`, `:84`, `:538-551` | M9: `slots` is 2 but the plan is single-slot, so it should report `partial` |
| **G10** | P2 | One abstaining family (for example unsupported Sleeper kicker scoring) makes every team-week with a teamed K incomplete in team_lineups-based tools. The abstention is correct, but the cause is not named per tool. | `team_lineups.py:225-229`; verified (probe) for one team-week | M6 Sleeper with an unsupported K key: team value and trades are all known-only, and the diagnostic must cite `kicker_projections` |
| **G11** | P3 | ESPN position = `defaultPositionId`; `eligible_slots` is ignored (Sleeper: `fantasy_positions` ignored). Frequency is UNVERIFIED. | `parse.py:207`, `:211` | a player with `eligibleSlots` [RB, WR] cannot fill WR |
| **G12** | P3 | A blank ESPN slot (id 22) is counted as an unrecognised starter by league_rules but as a non-starter by the parser and adapter. | `league_rules.py:207-212` vs `constants.py:12` | `lineupSlotCounts {"22": 1}`: rules starters +1 |
| **G13** | P1 (test debt) | No OP, multi-QB or 3-FLEX coverage in start_sit, what-if, waivers, trade_discovery, add_drop, kdst or action center. | test inventory above | M2 matrix |
| **G14** | P3 | `engine_support` has no stable code, and what-if keys on the English rule text. | `lineup_what_if.py:360` | rename the rule text and what-if loses the lock rule silently |

### Phase 3 status: G2, G3 and G4 (unsupported-slot integrity)

One shared mechanism: `lineup.lineup_slots` returns a `LineupSlots` list that also names the league's starting slots no
engine fills (`unsupported`: IDP, P, HC, unrecognised; bench, IR, taxi and the blank ESPN slot are never among them).
Synthetic fixtures M8 (ESPN LB + DL), M8p (ESPN P + HC) and M8s (Sleeper DL + LB + IDP_FLEX) pin the behaviour in
`tests/test_unsupported_slots.py` and `tests/test_league_compatibility.py`.

- **G2 fixed.** Start/Sit holds a starter in such a slot where he is: he is in neither total and never moved, so the
  projected gain compares like with like and is exact (M8: +0.9, where the old rule gave -18.5). `summary.lineup_coverage`
  names the slots, the held starters and, for Sleeper, the starters missing from the player data.
- **G3 fixed.** `team_lineups.project_week` marks such a week partial (`projection_complete` False,
  `projected_lineup_points` None, `unsupported_slots`), so every consumer (matchup, lineup projections, trades, add/drop,
  claim planner, FAAB, team value, power rankings, playoff race) shows known points only. A comparison stays exact
  (`team_lineups.delta_exact`) only when every such slot takes positions the engines never start and both rosters hold the
  same such players; otherwise it is known-only. Start/Sit and what-if totals are partial the same way.
- **G4 fixed.** `trades.roster_legality` makes legality `unverified` only where the roster count is unknown: the team
  has rostered players missing from the player data (Sleeper IDP: phantom open spots), or a league with such slots has
  no player count for the team. Then add/drop gives `legal` None with a `ROSTER_UNVERIFIED` note and no open-spot count,
  trade sides carry `legality` and no open-spot count, the claim planner marks the claim unverified and FAAB repeats the
  note. Where every rostered player is stored (ESPN, IDP/P/HC included) the count is exact and the open-spot numbers
  stay; dropping a player who can start only in such a slot gets an `UNSUPPORTED_SLOT_PLAYER` note (his value is not
  modelled). A count already over capacity stays illegal. The diagnostic reports `players_missing` (unverified) or
  `roster_legality` (handled, or unverified without rosters) for the tools that check capacity.

### Scoring-fidelity items left to #254 (inventory only)

- TE premium as an ESPN slot override: the skill model abstains, which is correct; ratings and xFP ignore it (a `partial`
  row exists).
- Kicker distance buckets and D/ST tiers: the resolvers abstain on unknown items; ESPN leagues fall back to ESPN's own
  K and D/ST numbers.
- FX-17's real scoring modifiers are unknown, so the fixture must not imply them.
- Cross-league fingerprint and cache isolation (`scoring_fingerprint`, `LeagueScope.cache_key`) was not re-audited
  here: UNVERIFIED.

---

## 6. Gated items (out of scope for #253)

- **Yahoo:** always disabled in the registry (`docs/multileague/provider-capability-matrix.md`). Add no OAuth,
  scraping or readiness claim.
- **IDP engines:** no IDP projections or model. #253 only labels IDP slots and players `unsupported` per tool (G2-G4)
  and never values them.
- **Provider access:** ESPN public import (`espn_public_import_enabled`) and private cookie access (encrypted credential
  path, `providers/espn.py:272-277`) are per-deployment flags. Whether the deployed environment has them is UNVERIFIED
  and was not checked. Add no new credential flow and no provider writes.
- **Dynasty picks:** `draft_picks` is synced for Sleeper but not valued (#223).
- **Also out of scope:** implementing scoring modifiers (#254), hypothetical scoring (#222) and live scoring (#173). The
  real FX-17 league's name, ids, members and rosters never enter the repository.

---

## 7. Recommended phase-2 PR (one small backend PR, after #249 merges)

1. `tests/compat_fixtures.py`: FX-17 ESPN and Sleeper builders plus matrix rows M1, M2a/b, M3, M4 and M8 (section 3.2).
2. `app/analytics/league_compatibility.py`: a pure `build_compatibility(lg, facts)` over `build_league_rules(lg)` plus a
   few cheap league-file facts: current-week projection coverage, `projection_source.select_run(...).unavailable_reason`,
   presence of a current lineup snapshot, and the count of rostered ids missing from `fantasy_players`.
3. Additive `code` on each `engine_support` row, with what-if switched to it.
4. The route `GET /api/league-compatibility` in `app/api/league_rules.py` (league-scoped through
   `mount_legacy_routers`), with an authorization test and golden updates.
5. `tests/test_league_compatibility.py`: FX-17 from import to rules to engines, checking the invariants (counts unchanged,
   17 unique starters, no BE, IR or taxi starter, 3 FLEX) and the per-tool statuses for M2-M4 and M8.
6. Fix **G1** in `start_sit.py` (about 5 lines), because without it the diagnostic would have to mark start_sit and the
   action center `partial` for every league with an unprojected starter-eligible player. Report G2-G4 and G6-G10
   through the diagnostic (`partial` or `unsupported`) and fix them in follow-ups.

Leave out: UI (a later small entry on the league-rules page), scoring changes (#254), E2E and goldens for new
leagues.

---

## 8. Phase 2 status (backend fixture and diagnostic PR)

Phase 2 implements section 7 on `origin/main` at `17d26e2` (after #249). Sections 1-6 above describe the code as audited
at `ea9e664` and are left as they were; line numbers in them may have moved.

### 8.1 What shipped

- **Fixtures.** `fantasy-intel/backend/tests/compat_fixtures.py` builds the matrix rows M1, M2 (FX-17 ESPN), M2b (FX-17
  with an unmapped stat and a TE slot override), M3 (FX-17 Sleeper), M4 (tied, sparse superflex with mixed flex types),
  M8 (ESPN IDP) and M8s (Sleeper IDP). It produces raw provider payloads, which go through the real
  `fetch_all`/`write_all` and `write_sleeper` paths. `espn_fixture` and the existing goldens stay untouched.
- **Diagnostic.** `app/analytics/league_compatibility.py` implements `league-compatibility/v1`.
  - `load_facts` reads the few league-file facts the diagnostic needs. It reads projections the way the engines do
    (`projection_source.league_projections` and `select_run`, never `weekly()`), as pinned in
    `tests/test_independent_projections.py`.
  - `build_compatibility` runs on top of `build_league_rules`.
- **Endpoint.** `GET /api/league-compatibility` lives in `app/api/league_rules.py` and is mounted league-scoped at
  `/v2/leagues/{league_id}/api/league-compatibility`. It is covered by the per-league golden check
  (`test_league_scope.py`) and by its own authorization test (404 for unknown or inaccessible leagues,
  `private, no-store`). Like every legacy route, it is also served on the classic `/api/league-compatibility` while
  `CLASSIC_SITE_ENABLED=1` (the owner's single-league site, reading that deployment's one league file), and answers 404
  there once `CLASSIC_SITE_ENABLED=0` (`app/main.py` `retire_classic_site`).
- **Stable codes.** Every `engine_support` row now carries a stable `code` (`league_rules.ENGINE_CODES`), and lineup
  what-if keys on `lineup_lock` instead of the English rule text (G14 fixed).
- **G1 fixed** in `start_sit.optimize_lock_constrained_lineup`. An empty recommended or unconstrained slot that a
  rostered, teamed, eligible player without a projection could fill now makes the total unknown. The total is `None`,
  the known part is still reported, and those players are listed under `missing_projections`. When no projection exists
  for the week, the source label is `"Unavailable"` (`start_sit.NO_PROJECTIONS`) instead of a provider name. Tests are
  in `test_start_sit.py` and `test_league_compatibility.py`.

- **Review fixes.** A verified independent run whose rows all abstain is reported as no projections: Start/Sit's
  label is "Unavailable" and the diagnostic's source is `none`, never Ours. A missing run is `unsupported` when nothing
  is shown (as for an ESPN week without numbers) and is reported for the families the models could project. G2 is worded
  as it behaves: a submitted lineup's total counts its IDP, P or HC starters while the recommendation cannot start them,
  so Start/Sit and what-if get a feature-level `unsupported` `gain_unfilled_slots` finding whenever such a slot holds
  a starter (the gain fix itself is phase 3). The Start/Sit page shows the missing marker ("No projections for this
  week") instead of a 0.0 submitted, recommended or unconstrained total, and the action center's decision context carries no
  projection source for such a week.
- **S1 label (no algorithm change).** `league_rules.skill_runs_rescored(con, lg)` checks whether the league's own skill
  runs carry `evaluation.rescored_from`, which `rescore.py` writes for a ratio re-score of reference-scoring runs. When
  they do, the `skill_projections` row is `partial` and says the projections are approximated by re-scoring. The
  diagnostic's matching finding carries `{"follow_up": "#254"}`. `rescore.py` is unchanged.
- **S8.** Start/Sit no longer labels a week without numbers "Ours". The `"Unavailable"` label survives the Sleeper
  wording rewrite (test). The team_lineups-based tools (`team_lineups.source_label`, used by trades, add/drop, team value,
  power rankings and K/D/ST streaming) still name "Ours" for a league whose default source is Ours even when no Ours run
  exists. Their totals are then known-only, and the diagnostic reports `projection_source: "none"` for them. Changing
  that label belongs to the owners of those engines and is listed as a follow-up.

### 8.2 Implemented contract (differences from the section 4 draft)

- `Entry` is `{status, reason, missing_input, provider_boundary, action, evidence}`. `evidence` lists every finding as
  `{code, status, value}`, handled findings included. The entry's status is the worst of its findings. Its reason joins
  the reasons of the findings that are not handled, most severe first.
- `projection_source` is `provider`, `ours` or `none`, or `null` for the bye planner. `ours_rescored` is left to #254,
  together with S1.
- The response has `contract`, `source` (the same as league_rules), `week`, `dimensions` (import, roster and scoring,
  league-wide), `tools` (13 tools with the four dimensions, `projection_source` and `refs` to engine_support codes),
  `unmapped_count` and a `note`. It returns no league, member or player identifiers and no player names.

### 8.3 Gaps reported by the diagnostic, left for follow-ups

| Gap | Finding code(s) and status | Follow-up owner |
|---|---|---|
| G2: Start/Sit gain is understated (the submitted total counts starters in slots no engine fills) | `unfilled_slots`, roster `partial`; `gain_unfilled_slots`, feature `unsupported` (start_sit, what-if) | #253 phase 3 |
| G3: full-lineup totals omit unfilled slots | `unfilled_slots`, roster `partial` (totals tools, waivers, bye planner) | #253 follow-up |
| G4: Sleeper IDP players missing from the player data | `players_missing`: import `partial`, roster `unsupported` for capacity tools, `partial` for others | #253 follow-up |
| G5: waiver lineup math treats a missing value as 0 and compares percentiles | `missing_value`, feature `partial` (waivers) | fixed in #260 (missing value unknown, never 0); the finding stays `partial` for the percentile basis and unknown players |
| G6: Sleeper waiver legality always unknown | `roster_restrictions`, roster `unverified` (waivers) | #180 agent |
| G7: ESPN IR statuses applied to Sleeper | `ir_eligibility`, roster `partial` (waivers, add/drop) | #180 agent |
| G8: sparse-pool replacement level 0 | `replacement_level`, feature `partial` (trade discovery) | the #182 agent owns trade code; diagnostic only here |
| G9: one streaming slot for 2+ K or D/ST | `streaming_slots`, roster `partial` (kdst_streaming) | #253 follow-up |
| G10: one missing family makes lineup totals known-only | `projection_coverage`, scoring `partial`; `totals_known_only`, feature `unverified` | #253 diagnostic; scoring fix #254 |
| S2: waiver values are full-PPR usage percentiles | `player_value_basis`, scoring `partial` with refs `ppr` / `te_premium` | #254 (the waiver warning was widened in #260) |

Other follow-ups:

- `team_lineups.source_label` should say there are no projections when the league's default source (Ours) has no
  verified run (S8 outside Start/Sit; it also reaches the add/drop and trade engines, which other agents own).

- G11 and G12 (dual eligibility, blank ESPN slot 22) are not yet reported by the diagnostic.
- The action center card shows "no projections for this week" for the new `"Unavailable"` label
  (`frontend/lib/action-center.ts` `projectionsNote`), shipped in this PR. `sectionContext` still maps any other label
  to `espn` in the decision context, which no wording reads today (`lib/weekly-decisions.ts` only types it).
- The frontend `EngineRule` type can gain the optional `code` when the UI entry for the diagnostic is built (no UI in
  this PR).
- UI entry point and E2E acceptance items for #253 remain open.

---

## 9. Completion audit (against `origin/main` at `dc06428`, plus the journey PR)

Each #253 completion criterion (`docs/roadmap-execution/STATUS.md`) checked against the code and tests on main after
phases 1-3 (784b89b, #256, #268), plus what the journey PR adds. Backend test paths are under
`fantasy-intel/backend/tests/`. "Import to analysis" means the row's raw provider payload goes through the real sync
write path (`write_all` for ESPN, `write_sleeper` for Sleeper) into a league file that the engines then read.

| Criterion | Status | Evidence, or the gap |
|---|---|---|
| Per-tool gap inventory committed | **met** | this document, sections 1-8 (784b89b, updated in #256 and #268) |
| Standard redraft | **met** | M1: `test_league_compatibility.py::test_every_matrix_row_keeps_its_slot_counts`, `::test_a_league_without_projections_gets_no_total_and_an_honest_source_label`, `::test_a_team_without_a_synced_roster_makes_capacity_tools_unverified`; `test_unsupported_slots.py::test_a_standard_week_keeps_its_exact_shape`, `::test_a_standard_league_keeps_legal_moves_legal` |
| Superflex / multi-FLEX with ties and sparse pools | **met** | M2 and M3 (FX-17), M4 (OP + RB/WR + WR/TE + 2 FLEX, all tied, no free agents): `::test_fx17_start_sit_starts_17_distinct_players_with_three_interchangeable_flex`, `::test_fx17_weekly_lineups_never_start_bench_ir_or_a_player_twice`, `::test_tied_and_sparse_superflex_lineups_are_deterministic_and_unique`, `::test_a_sparse_pool_flags_the_trade_replacement_level` |
| Half and full PPR | **met (this PR)** | M1/M2 (ESPN full), M3 (Sleeper half), new M5 (ESPN half) and M5s (Sleeper full): `::test_ppr_variants_survive_the_import_and_the_full_ppr_waiver_basis_is_disclosed` (format and PPR read from the import, `ppr` row partial only when PPR is not 1, waivers' full-PPR basis cited, ESPN projections stay league-scored) |
| Known and unmapped custom scoring | **met (this PR)** | new M6 (ESPN 6-point passing TD): `::test_known_custom_scoring_is_read_and_espn_projections_stay_league_scored` (caveat: M6 proves that the rules page reads the custom rule and that ESPN's own league-scored projections are used; it stores no Ours run, so it does not exercise an Ours re-score, which is S1 and #254's); M2b (ESPN unmapped stat + TE slot override): `::test_roster_support_is_not_scoring_fidelity`; new M6s (Sleeper `bonus_pass_yd_300` + an unknown key): `::test_unmapped_custom_scoring_abstains_and_never_becomes_a_measured_zero` |
| IR / taxi | **met (this PR adds FX-17 + taxi)** | FX-17's 3 IR in the two FX-17 lineup tests above; new M7 (FX-17 on Sleeper with 3 taxi per team, taxi players projected far above everyone): `::test_fx17_with_a_taxi_squad_never_starts_taxi_or_ir_and_keeps_them_off_the_active_roster` (caveat: Sleeper publishes no projections and M7 stores no Ours run, so the test injects provider-style rows and reads them through the ESPN path, `source="espn"`, as `test_sleeper_taxi.py` does; it pins lineup and capacity behaviour, not the Ours path); `test_sleeper_taxi.py` (recorded-shape Sleeper fixture through `write_sleeper` into Start/Sit, trades, waivers, team value) |
| IDP / atypical slots | **met** | M8 (ESPN DL/LB), M8p (ESPN P/HC), M8s (Sleeper DL/LB/IDP_FLEX): all of `test_unsupported_slots.py`; `test_league_compatibility.py::test_idp_slots_are_partial_for_lineups_and_unverified_legality_only_when_players_are_missing`, `::test_punter_and_head_coach_slots_are_reported_unsupported` |
| FX-17 invariants (counts unchanged, no double start, 3 interchangeable FLEX, no BE/IR start, totals and displacement in tied, sparse and multi-flex pools) | **met** | `::test_fx17_slot_counts_survive_normalization_on_both_providers`, the two FX-17 lineup tests, the M4 test and the M7 taxi test above (totals equal the sum of the assigned starters); displacement with slots no engine fills: `test_unsupported_slots.py::test_start_sit_holds_starters_in_unsupported_slots_and_the_gain_is_exact` |
| Every applicable engine answers or abstains with an actionable reason | **met except trade discovery on large rosters** | engine level: Start/Sit, what-if, matchup, projected lineups, trades, trade discovery (M8, M8p), add/drop, claim planner, FAAB, team value, power rankings and playoff race in `test_unsupported_slots.py` and `test_league_compatibility.py`. Route level (new): `::test_every_tool_answers_or_abstains_with_a_reason_from_the_imported_file` serves the imported file of M2, M3, M6s, M7, M8p and M8s and calls 18 tool routes (plus `/api/trades/discover` on the standard-size rows M6s, M8p and M8s) and an evaluated trade. Statuses are pinned: every route answers 200 except `/api/waivers` on the Sleeper files (M3, M6s, M7, M8s), which abstains with 503 "not computed yet, run the analytics refresh" because the analytics pipeline has not run on them; any other status fails the test. The diagnostic answers all 13 tools on four dimensions for every one of the 13 matrix rows (`::test_every_row_answers_every_tool_on_four_dimensions`). **Not met for trade discovery on FX-17-sized leagues:** `GET /api/trades/discover?team_id=1` on M2 (6 teams, 30 players each, 17 starters with OP and 3 FLEX) took **451 s** offline (about 6 s on the standard-size rows), with no budget and no abstention, so it neither answers in a usable time nor abstains. The diagnostic still reports `trade_discovery` as `handled` for FX-17 and is deliberately not changed here; the fix decides how to report it. Tracked as STATUS follow-up **1p**, fixed separately on branch `fix/trade-discovery-budget` (a disclosed search budget). Team value's trade preview took 19 s on M2 (`trades=false`: 0.6 s); the route test calls it with `trades=false` and says so at the test. |
| Missing or unsupported scoring never becomes a measured 0 or a faux league-scored total | **met for #253's scope, one disclosed exception (#254)** | G1: `::test_a_league_without_projections_gets_no_total_and_an_honest_source_label`, `::test_a_starter_slot_only_an_unprojected_player_can_fill_makes_the_recommendation_incomplete`; S8: `::test_an_unavailable_source_label_survives_the_sleeper_wording`, the two "rows all abstain" tests; S1 label: `::test_ours_re_scored_from_the_reference_scoring_is_partial_not_league_scored`; G3: `test_unsupported_slots.py` partial totals; G5 (#260): `test_waivers.py::test_player_without_value_leaves_lineup_value_unknown_not_zero` and its neighbours; new M6s test (every lineup total None, never 0.0). "Never" has one disclosed exception, owned by #254: S1b, where the Ours ratio re-score counts a missing box-score component as 0 (labelled partial since #256, algorithm unchanged). S3 (Sleeper actual points mix sources per row) is also #254's. |
| Per-league / per-tool diagnostic contract (D-12) with tests and readable explanations | **met (backend)** | `app/analytics/league_compatibility.py`, `GET /v2/leagues/{lg}/api/league-compatibility`; `test_league_compatibility.py` (`well_formed`, reasons asserted word for word for G2/G3). The diagnostic itself has no UI entry; the League rules page discloses the same slot and rule support (`Lineup slots`, `Engine support`), which the journey checks. |
| `league_scope` / tenant isolation on the fixtures; no provider writes, no credentials | **met** | `::test_v2_league_compatibility_is_league_scoped` (404 for unknown and inaccessible leagues, `private, no-store`, no league id in the body), `test_league_scope.py` (per-league goldens of every mounted route), `test_league_scope_audit.py`. These backend tests are the guarantee. The journey adds only a browser-side check: no request from the browser to a provider host and no non-GET API call from the browser; it cannot see what the Next server or the API does. The fixtures carry fake ids and names only. |
| One responsive, accessible E2E: connect/demo → league rules → lineup/waivers/trades, with an unsupported case | **met (this PR; CI pending)** | `fantasy-intel/frontend/e2e/compat-journey-multileague.spec.ts`, run by `playwright.multileague.config.ts` (the CI "multi-league parity + UX gates" suite picks it up with no workflow change), at 1440 and 375 px: demo → connect page (waited on its own content, since its skeleton renders the same h1) → ESPN fixture league (rules all handled, complete Start/Sit totals, add/drop counts the roster, trade needs no drop) → Sleeper IDP league (`build_e2e_sleeper.py --idp`: DL and LB "Not applied" on the rules page, Start/Sit totals marked `*` partial with the slots named and their known part above 0, no projection cell reading 0, add/drop and trade say roster legality is unverified, both sides' trade totals `pts* (partial)` and above 0, every missing value a bare dash with a reason). Each page, after its content waits: one h1, landmarks, decision first, no sideways scroll, app bar fits, axe in dark and light, no console error; a keyboard pass on three pages. |
| Exact-head CI green, independent security review (D-10), merged | **not met** | pending on the journey PR |

**Found by the journey, fixed in #284:** on the multi-league connect page, the unavailable ESPN host card showed
the provider's operator reason, which named deployment settings (`CREDENTIALS_PUBKEY_V<n>`,
`ESPN_PUBLIC_IMPORT_ENABLED`) to visitors. #284 replaced it with plain visitor copy in
`backend/app/leagues/providers/espn.py` (the setting names stay in a code comment for operators) and removed its
`CONTENT_TRIAGE` entry from `frontend/e2e/ux-triage.ts`, so the UX gate fails if it returns.

**Found by the journey and fixed in the same PR:** the demo page's "Connect a league" link in muted running text was
told apart by colour only (axe `link-in-text-block`, 1.01:1 against the surrounding text); links inside `.plain` text
on the league pages are now underlined (`app/leagues/leagues.module.css`).

**Follow-ups (not done here; engine or ownership boundaries):**

1. Trade discovery on FX-17-sized leagues: 451 s for one request offline, no budget and no abstention. STATUS
   follow-up 1p, branch `fix/trade-discovery-budget` (a disclosed search budget). That fix also decides what the
   diagnostic reports for `trade_discovery` on such leagues (`handled` today).
2. Team value's trade preview: 19 s on FX-17, the same search; covered by follow-up 1p.
3. Still open from section 8.3 and the triage table: G6/G7 (Sleeper waiver legality and IR rules, #180 owner), G8
   (sparse replacement level, diagnostic only), G9 (2 K or 2 D/ST streamed as one slot), G10 (one abstaining family
   makes totals known-only, named by the diagnostic), G11/G12, and `team_lineups.source_label` naming "Ours" with no
   Ours run. Scoring fidelity (S1b, S2 basis, S3) stays with #254. **#254 phase 2:** S1b closed for the kicker
   re-score and the Sleeper box score, S3 per-row `actual_source`, and `team_lineups.source_label` "Unavailable" for
   every caller outside the trade evaluation and trade alternatives (`254-scoring-support.md` section 7).
4. The `league-compatibility/v1` diagnostic has no page of its own. The League rules page already discloses slot and
   rule support; a per-tool table there is optional (issue #253 step 5: "only if necessary").

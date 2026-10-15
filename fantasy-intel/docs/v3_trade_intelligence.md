# V3 Trade Intelligence: flexible trades, live roster selection, practical discovery, injury-aware valuation

Code:

* `backend/app/analytics/trades.py`: the evaluator, the shared forced-drop chooser and injury scenarios.
* `backend/app/analytics/player_availability.py`: NFL availability.
* `backend/app/analytics/trade_discovery.py`: the exact search plus the beam for larger shapes.
* `backend/app/analytics/trade_assets.py`: asset balance and roster needs.
* `backend/app/analytics/trade_practical.py`: modes, tiers and rationale.
* `backend/app/api/trade_discovery.py` and `backend/app/api/routes.py`: the APIs.
* Frontend: `frontend/app/trades/page.tsx`, `frontend/app/trades/TradeBuilder.tsx`, `frontend/lib/trade-builder.ts` and
  `frontend/app/trades/discover/page.tsx`.

Tests:

* `backend/tests/test_trade_v3.py`, plus the updated V2 suites;
* `frontend/lib/trade-builder.test.ts` (`npm test`).

What did not change:

* The canonical value of a trade is still exactly what it does to both teams' optimal starting lineups. It uses
  ESPN's weekly projections, exact FLEX rules and the regular-season / playoff split.
* Missing projections are never 0, and kickoff-aware start weeks still apply.
* Nothing writes to the database or to ESPN.
* Player BUY/HOLD/SELL signals, waiver calculations and manager analytics are untouched.

Nothing in V3 produces an acceptance probability, a trade grade or a market price.

## 1. Flexible trade limits (manual analyzer)

The V2 allowlist `SUPPORTED_SHAPES = {1-1, 2-1, 1-2, 2-2}` is gone. `trades.validate` accepts any number of players
from 1 to `max_per_side` on each side, for example 3-for-1, 1-for-3, 3-for-2, 4-for-2, 4-for-4 or 6-for-6.

* The default per-side limit is **6** (`trades.MAX_PLAYERS_PER_SIDE`). The API reads
  `TRADE_MAX_PLAYERS_PER_SIDE` (clamped to 1–10) and `/api/trades/options` reports it to the page.
* The limit is a performance guard, not a rule of the game. Six still evaluates in about 0.3 s, even with five
  forced drops (see §12).
* Validation errors:
  * same team;
  * unknown team;
  * a player listed twice (on either side);
  * a player not on the sending roster;
  * an empty side;
  * more than the limit.
* Everything after validation is the canonical engine:
  1. remove the outgoing players;
  2. add the incoming players (active);
  3. enforce capacity and ESPN position limits with forced drops;
  4. rebuild both optimal lineups for every remaining week;
  5. report weekly, regular-season, playoff and rest-of-season deltas.

Each side now also reports:

* `trade.team_a_sends_count` and `trade.team_b_sends_count`;
* `roster.issues_before_drops`: how far the post-trade roster exceeds active capacity, and which positions exceed
  ESPN's limit;
* `roster.forced_drop_search`: the number of drops, candidate drop sets, lineup solves and the method used.

## 2. Roster legality and forced drops

The rule is unchanged from V2:

1. Use the smallest number of removals that makes the roster legal.
2. Among those, keep the set that leaves the largest known projected starting total (ties: lowest player ids).
3. Never drop an incoming player unless no legal set without one exists.
4. Report a cheaper set that would cut an incoming player as a "redundant incoming asset".

The rule is enforced against active capacity and ESPN position limits (`legal_roster`). A limit the team already
exceeded before the trade is not blamed on the trade. IR slots are unchanged: current fantasy-IR players stay
rostered and inactive, and incoming players arrive active.

V2 enumerated at most three drops. V3 resolves any number exactly, with `trades.choose_forced_drops`, which the
evaluator and discovery both call:

* **Lower bound.** The objective is the sum over weeks of a maximum-weight lineup assignment. With non-negative
  projections, that is a weighted-matroid (gross-substitutes) value: monotone and submodular in the roster. So
  removing a set D costs at least the sum of its members' single-removal costs:
  > L(R) − L(R \ D) ≥ Σ_{d∈D} [L(R) − L(R \ {d})]

  This follows by telescoping the marginal values and applying submodularity.
* **Minimum size.** The fewest possible removals is max(capacity excess, Σ position excesses). Each removal frees
  at most one active spot and lowers one position count.
* **Branch and bound.** Legal sets of that size are solved in increasing order of the bound. The search stops once
  the bound exceeds the best exact cost plus 1e-6, so no unsolved set can beat or tie the choice. A negative
  projection voids the bound, and then every legal set is solved.
* **Budget.** The search allows at most 250,000 candidate sets (`MAX_DROP_SETS`) and 5,000 lineup solves
  (`MAX_DROP_SOLVES`). Beyond that it raises `EvaluationUnavailable` (HTTP 422, "evaluation unavailable") instead of
  approximating. Discovery counts such candidates as `excluded_evaluation_unavailable`.

The tests check every choice against full brute-force enumeration:

* random rosters with 1–3 drops;
* a 1-for-5 that needs 4 drops;
* position-limit drops;
* negative projections.

All V2 brute-force tests of discovery still pass on the shared chooser.

## 3. Reactive trade selection (`/trades`)

`TradeBuilder` is a small client component, and the page around it is still server-rendered. The page loads every
roster once, through `/api/trades/options` on the server, and passes them to the builder.

* Team A, Team B, the start week and both selections are **controlled local state** (`lib/trade-builder.ts`, pure
  functions).
* Changing a team swaps the already-loaded roster instantly. No request is made: Playwright confirmed no document
  or RSC request for `/trades` was sent.
  * That side's picks that are not on the new roster are cleared.
  * The other side's picks are kept.
* Choosing the same team twice shows "Team A and Team B are the same team" and disables Evaluate.
* The shape (`4-for-2`), the counts and capacity/position warnings update as players are ticked. The warnings
  include "at least 2 forced drops" and "WR over ESPN's limit of 4".
* **Evaluate** is the only action that evaluates. It navigates to the URL-backed selection
  (`/trades?ta=&tb=&a=&b=&sw=`), and the server calls `POST /api/trades/evaluate` and renders the result.
  * No backend call is made from the browser, so no environment variable or credential reaches it.
  * The existing server-side `lib/api.ts` is not imported into client code.
* Deep links from Trade Discovery and from My Team Value (PR #14) keep working. A link is a deliberate selection,
  so it is evaluated on load.
* **Stale results.** The rendered result is tagged with the selection it belongs to. After any edit (team, player
  or start week) it is hidden, and the page says "The selection changed since the last evaluation…". The old result
  can be shown dimmed and labelled as a different trade. While the navigation is pending the page shows
  "Evaluating…".

The result adds:

* a roster-limits panel: capacity and position limits before drops, forced drops with their exact search statistics;
* starting opportunities: who becomes a starter, which starters leave, who gains or loses starts;
* the injury warnings and return scenarios (§11).

## 4. Practical versus exploratory discovery (`/trades/discover`)

Modes (`trade_practical.MODES`) never change the canonical valuation. They choose which exact rows are shown, in
what order, and with which evidence.

| Mode | What it shows | Order |
|---|---|---|
| **Practical** (default) | Both lineups improve in the chosen phase. The asset packages are not lopsided against the other manager. Injured or incompletely projected packages are set apart. | the smaller side's canonical gain |
| **Mutual improvement** | Every exact trade in which both lineups gain in the selected phase (complete projections). The asset diagnostic is shown, not filtered. | the smaller side's gain in that phase |
| **Maximum upside** (exploratory) | Largest gain for you. Their losses and asset imbalances are labelled prominently. | your gain |
| **All candidates** (advanced) | Any sort (`my_gain`, `their_gain`, `combined`, `min_side`, `playoff_gain`, `min_side_regular`, `min_side_playoff`) and any V2 category. | the chosen sort |

### Practical tiers

`trade_practical.classify` uses only the canonical deltas, the asset diagnostic and availability.

* **practical**: both deltas complete and positive in the phase; no asset concern; no injury dependence.
* **concerns**: both improve, but at least one of these applies:
  * they give up moderately more;
  * you surrender far more, or multiple strong assets;
  * a known NFL-ineligible stretch;
  * a fantasy-IR player is involved;
  * a forced drop.
* **speculative**: incomplete projections; asset data insufficient; a player with unknown NFL eligibility or an
  unconfirmed return; or receiving a fantasy-IR player.
* **Excluded** from practical results (kept in the other modes):
  * one lineup does not improve;
  * a significant asset imbalance against them;
  * they surrender multiple strong assets for fewer.

Strictness controls the asset filter:

* `strict` also excludes moderate imbalances against them;
* `standard` is the default;
* `off` keeps the labels but filters nothing.

### Pool and display

* The practical pool is the **exact top 60** trades by smaller-side gain among mutual-improvement trades, plus the
  heuristic larger-shape rows. A proven top list remains proven within the exact shapes.
* Display diversity: at most 2 variants per manager and set of players received, and 4 per manager. Hidden
  variants are counted (`excluded.similar_variants`), never re-ranked.
* Phase control: rest of season, regular season or fantasy playoffs. This selects the delta used for the tier and
  for the order. Trades qualify for discovery only when your ROS or playoff delta is positive, as in V2.

A trade in which the other manager loses lineup production never appears in the default mode. It is still visible,
labelled, in Maximum upside and All candidates, and any trade can be opened in the analyzer.

## 5. Asset-package balance methodology (`trade_assets.py`)

This is a diagnostic that is independent of the lineup delta. Lineup impact asks what a trade does to each team's
starting lineups. Asset balance asks how much position-relative value each manager surrenders, whatever their bench
congestion.

* **Points above replacement (PAR)** = Σ over remaining weeks in which the player is NFL-eligible of
  max(0, ESPN projection − replacement level at his position that week).
  * Units: projected fantasy points above replacement over the evaluation window, also shown per week.
  * No weights, no star premium, no raw cross-position comparison.
* **Replacement level** is what a manager can get for free: the best currently available free agent's ESPN
  projection at that position that week. This is a streaming baseline, and it is availability-aware.
  * If no free agent at the position is projected, the league-structure level is used instead: the projection of
    the first player beyond the league's starters at that position (teams × fixed slots).
  * The baseline used is shown for each player.
* **Starter-caliber**: PAR ranks within the league's starters at the position (teams × fixed starting slots). This
  is how a benched top asset keeps independent context. For example, a TE who never starts for his own team can
  still be TE2 of the league and starter-caliber.
* **Labels**, based on the two packages' PAR (you send / they send):

  | Label | Rule |
  |---|---|
  | `insufficient_data` | any moved player lacks a projection in an eligible week, or his NFL eligibility is unknown. Never "balanced". |
  | `replacement_level` | both packages are under **1.0 point per remaining week** above replacement |
  | `they_surrender_multiple_strong` / `you_surrender_multiple_strong` | one side sends ≥ 2 starter-caliber players, receives fewer, and gives more PAR |
  | `balanced` | the smaller package is ≥ **2/3** of the larger |
  | `they_give_more` / `you_give_more` | moderate imbalance: ratio between **0.4** and 2/3 |
  | `significant_they_give_more` / `significant_you_give_more` | ratio < 0.4 |

The thresholds are display heuristics chosen to be readable. A difference of about a third or less between two
packages reads as "relatively balanced"; less than 40% reads as a clear mismatch. They were **not** fitted or
calibrated against real trades, and the page states their values. They are labels, not a market price. They never
change a canonical delta and never hide a trade from the manual analyzer.

## 6. Counterparty needs and rationale

* **Roster needs** come from the canonical lineups (`trade_assets.league_needs`). For each position, the measure is
  the team's fixed starters' projected points per slot-week and its rank in the league.
  * **Short** means the bottom half.
  * **Surplus** means a starter-caliber player who starts at most half the remaining weeks.
* The stored manager-scouting evidence (needs, roster construction, trade history and confidence) is still attached
  as secondary context.
* Every shown trade gets evidence-backed statements (`trade_practical.rationale`). Concerns are shown first, in a
  callout.
  * Lineup usage: "You gain a projected RB starter: … starts 11 of 12 weeks"; "Their WR position improves…";
    "… would not start for them".
  * "Both teams' projected starting-lineup totals improve (you +x, them +y)"; "Their projected starting lineup
    loses …".
  * "They surrender a player who currently starts in most weeks"; "You give up a regular starter".
  * "The asset packages appear uneven despite the lineup benefits…".
  * Needs: "You are short at RB…"; "Team X is short at WR… A roster need is not a willingness to trade"; "… is
    surplus for you"; "Team X can spare …".
  * Holes and drops: "… would have an empty TE slot in week(s) … after the trade"; forced drops.
  * Injury warnings (§11) and missing projections.
  * Trade type: consolidation, depth for a starter, positional exchange, or like-for-like.

## 7. D/ST and K policy

**Reproduced** with synthetic leagues (`test_dst_driven_gains_no_longer_lead_discovery`, and the evidence in the
PR):

* In a 12-team synthetic league, 9 of the V2 default top 10 (sort `my_gain`, all positions) moved a K or D/ST, and
  10 of 10 cost the other lineup.
* The #1 row was "your D/ST for their RB1 + their D/ST": +171 for you, −152 for them.

**Causes found:**

1. **Sorting by one-sided gain.** Nothing stopped "upgrade my defense and take your RB1" from ranking first.
2. **D/ST as a slot-filling vehicle.** Each team rosters about one D/ST, so a D/ST-for-D/ST swap keeps both slots
   filled. That makes lopsided packages look structurally clean.
3. **Matchup-driven ESPN weekly D/ST projections.** Roughly 2–15 points, with large week-to-week swings, so a
   second defense "streams" with perfect foresight inside the optimizer.
4. **No streaming credit.** The canonical delta does not credit the team losing a D/ST with the free-agent
   defense it could stream.

No projection was scaled.

**Policy:**

* Automatic discovery defaults to QB/RB/WR/TE (`trade_discovery.DEFAULT_POSITIONS`). K and D/ST players are not
  traded in candidates, but they stay on rosters and in lineups.
* `include_kdst` (the page checkbox "include K and D/ST") or `positions=` adds them, and then nothing is silently
  excluded.
* Their asset value is measured above the best available free agent at the position each week (the streaming
  baseline, shown per player). So "a large projected D/ST improvement alone" does not make a lopsided skill
  package practical: the one-sided rows above are excluded from practical results because the other lineup loses.
* The manual analyzer always accepts K and D/ST.

## 8. Expanded search methodology

**Exact shapes (unchanged proofs).** 1-for-1, 2-for-1 and 1-for-2 still use:

* stage A's provable upper bounds;
* stage B's best-first evaluation with a proven top K;
* stage C's check against the manual evaluator.

V3 adds per-phase bounds (`min_side_regular` and `min_side_playoff` are minimums of the same weekly bounds summed
over the phase, so they are still upper bounds) and the position filter. The V2 brute-force equality tests run
over every position.

**Larger shapes (heuristic beam, `TradeSearch.beam`):** 2-for-2, 3-for-1, 1-for-3, 3-for-2 and 2-for-3 by default;
3-for-3 when requested. The stage-A bounds are not extended to these shapes: the two-player bounds rely on
pairwise exact solves, and enumerating every 3-for-3 in a 12-team league would mean billions of packages. So no
proof is claimed. Per opponent:

1. Compute signals from exact lineup totals: each player's cost to his own team (removal), what he would add to the
   other team (addition), and his own known points.
2. Build pools: on each side, the 4 players who add most to the other team plus the 4 with the most surplus (own
   points minus own-team cost).
3. Seed: the 6 pool 1-for-1 pairs with the largest approximate smaller-side gain, evaluated exactly.
4. Expand: add one pool player to either side and evaluate every new package exactly. This is the same `_side` as
   stage B, with forced drops and NFL availability. Keep the 6 best by exact smaller-side gain, and repeat up to the
   largest requested shape.
5. Deduplicate by sorted player sets. Skip padding: an added player that changes neither team's effects or drops.
   Illegal rosters are rejected by the shared forced-drop rules. Packages that qualify become rows marked
   `search_method: heuristic_beam`.

Every displayed row, exact or heuristic, is re-run through `evaluate_trade` and carries
`consistent_with_manual_evaluator`.

## 9. Exact versus heuristic coverage

The API returns `coverage.exact` (the V2 search block, which is exhaustive within the exact shapes) and
`coverage.heuristic`:

* `method`;
* `exhaustive: false`;
* packages evaluated;
* the beam parameters;
* per-opponent seeds, pools and evaluations;
* timing;
* a note that it is not a proven top list.

The page shows both as separate tiles, and marks heuristic shapes with `*` in the controls and with a
"heuristic search" badge on each row. There is also an "exact search only" option.

## 10. API changes

**`GET /api/trades/discover`** parameters:

* `mode` (practical | mutual | upside | all);
* `phase` (ros | regular | playoff);
* `strictness` (strict | standard | off);
* `include_kdst`, `positions`;
* `shapes` (exact and heuristic, comma-separated; the default is everything except 3-for-3);
* `sort` and `category` (all mode);
* `opponent`, `limit` (≤ 50), `heuristic`.

The response:

* `sections` (practical, concerns, speculative; or `results`) of fully evaluated rows. Each row has
  `asset_balance`, `tier`, `tier_reasons`, `injury_flags`, `injury_scenarios`, `rationale` and `search_method`;
* `excluded` counts, `pool_rule`, `coverage`;
* `opponents` (best row in the view and lineup-based needs), `my_needs`;
* asset notes and thresholds, `kdst_policy`, `availability_note`.

`results` (all shown rows) and `search` remain for compatibility.

**`POST /api/trades/evaluate`:** any shape up to the limit. It adds the counts, `issues_before_drops`,
`forced_drop_search`, `players[].availability`, `injury_warnings`, `injury_scenarios` and `availability_note`.

**`GET /api/trades/options`:** each roster player gets `nfl_ineligible_weeks` and `availability`. The response adds
`max_players_per_side` and `availability_note`.

**Python:**

* `TradeSearch(..., positions=DEFAULT_POSITIONS)` now leaves out K and D/ST unless `positions=None` or a list is
  given. PR #14's 1-for-1 trade preview therefore follows the same automatic-discovery policy.
* `TradeSearch.top` keeps its signature.
* `trades.SUPPORTED_SHAPES` and `MAX_FORCED_DROPS` were removed.
* `RosterPlayer` gained `unavailable_weeks`, and `LeagueState` gained `availability`.

## 11. Injury-aware valuation

**Root cause (audit).** `side_lineups` activates incoming players (`replace(p, on_ir=False)`) because a traded
fantasy-IR player cannot be assumed to keep an IR slot. V2 had no notion of NFL eligibility, and ESPN's weekly
projections were the only input.

* A fantasy-IR player was worth nothing to his current team (IR held as synced), but the receiving team was credited
  with every positive ESPN projection from the first week.
* A player on NFL injured reserve but in an ordinary bench slot was credited by **both** teams in every week ESPN
  projects him.
* ESPN's own data for the reported case, read on 2026-10-15 via the ESPN league connector (not committed, not
  stored): Travis Etienne Jr., `injuryStatus = INJURY_RESERVE`, season projection 124.9 against 30.9 actual points.
  That is consistent with positive future weekly projections while he is on IR. ESPN's per-week projections were not
  available through that read, so the exact weeks 6–8 values are unconfirmed, and no local synced database exists in
  this environment.

**Concepts are tracked separately** (`player_availability.derive`, one record per player):

| Field | Source | Meaning |
|---|---|---|
| `espn_injury_status`, `espn_status_synced_at` | `fantasy_players.injury_status`, `detail_synced_at` | ESPN's designation (ACTIVE, QUESTIONABLE, OUT, INJURY_RESERVE…) |
| `nfl_designation`, `_label`, `_source`, `designation_since_week` | nflverse `nfl_rosters_weekly` (`status` RES + GSIS `status_description_abbr`: R01/R48 IR, R04 PUP, R05 NFI) | NFL reserve list and the stint start |
| `fantasy_ir_slot` | ESPN lineup slot | whether he occupies a fantasy IR slot |
| `earliest_eligible_week`, `eligibility_basis` | rule + `nfl_schedules` | the minimum eligible week, with the games counted |
| `expected_return_week`, `expected_return_source` | none synced | always `None`: no reliable return-date source exists in the data |
| `return_confirmed` | later weekly roster back to ACT | activation observed |
| `unavailable_weeks`, `ignored_projection` | derived | verified NFL-ineligible weeks, and the ESPN projections not counted in them |

**The rule:**

* IR, PUP and NFI in season require at least **4 team games** missed.
* The stint start is the first weekly roster of the current run showing the player on reserve. That week's game is
  counted as missed, and a week with no roster row counts as possibly on reserve. Both choices make the derived
  earliest week no later than the true one, so the claimed ineligible weeks are a lower bound.
* With the team schedule, the earliest week is the week of the 5th game from the start, so byes push it later.
  Without the schedule it is start + 4.
* A team's schedule is used only when it is complete: the loaded regular season spans at least 17 weeks and the
  team plays every week but one (its bye). A partially loaded schedule falls back to start + 4, because missing
  rows are not games that don't exist, and treating them as such would delay or deny an eligible week.
* A reserve code that is not in the mapping (for example a suspension or other list), or an ESPN-only
  `INJURY_RESERVE`, gives status `reserve_eligibility_unknown`, and no week is treated as ineligible.

**Statuses:**

* `nfl_ineligible`;
* `eligible_return_unconfirmed`, meaning past the minimum week but no activation seen;
* `no_eligible_week_remaining`;
* `reserve_eligibility_unknown`;
* `returned_from_reserve`;
* `available`.

**Lineup engine (shared).** `load_league_state` applies `unavailable_weeks` once, so the evaluator, discovery, My
Team Value (when PR #14 merges) and the Playoff Race projections all use the same weeks.

* In those weeks the player cannot start (`project_week` lists him under `unavailable` with ESPN's projection).
* It is not a measured 0, and a missing projection in such a week does not make the week incomplete.
* Discovery's totals solver applies the identical rule, so it stays bit-identical with the evaluator.
* After the earliest week nothing is assumed: an eligible week is not a return, and ESPN's projection is used as
  published. So a Week-9 return keeps its full post-return and playoff upside.

**Scenarios (`injury_scenarios`).** For each traded player whose availability is restricted or unknown, the exact
simulation is re-run for both teams with only his availability changed:

* return at the earliest eligible week (or, eligibility unknown, "as ESPN publishes");
* a **hypothetical** return delayed to the first fantasy playoff week (a reference point, not a forecast);
* no contribution for the rest of the season.

The canonical row is marked. No probabilities are attached and there is no universal injury haircut.

**Discovery treatment:**

* Ineligible weeks already contribute nothing to the canonical deltas and to asset PAR.
* Practical mode puts trades that depend on an unknown-eligibility or unconfirmed-return player, or on receiving a
  fantasy-IR player, in **speculative**. Known ineligible stretches go to **concerns**.
* Warnings read, for example: "Currently on NFL reserve/injured. Earliest eligible return week 9; actual return not
  confirmed. Near-term lineup contribution (weeks 6-8) is unavailable."
* Stale data is flagged: "NFL roster data was last synced for week N…", and "ESPN still projects X points in weeks
  6-8 while he is NFL-ineligible; not counted".

**Data gaps:**

* No expected-return feed.
* The GSIS code mapping is from the NFL's published codes and needs confirming on real synced rosters.
* nflverse weekly rosters lag real transactions by up to a week.
* ESPN may keep positive projections for reserve players.
* Teams can activate a player later than the earliest week, or never.

## 12. Performance

Synthetic leagues: 16 active players and 1 IR per team, 12 remaining weeks, 150 free agents. Default shapes
(exact plus 2-for-2, 3-for-1, 1-for-3, 3-for-2, 2-for-3), QB/RB/WR/TE. Single process, no tracemalloc.

| Teams | Exact candidates (screened) | Exact evaluations for practical | Beam packages evaluated | Stage A | First practical request (incl. beam) | Beam | Mutual / upside (cached search) | + 3-for-3 beam |
|---|---|---|---|---|---|---|---|---|
| 10 | 30,375 (12,929) | 96, proven by bounds | 1,621 | 0.57 s | 3.85 s (2.12 s) | 2.12 s | 0.61 / 1.24 s | 2.9 s |
| 12 | 37,125 (15,844) | 98, proven by bounds | 2,021 | 0.74 s | 4.22 s (2.42 s) | 2.42 s | 0.66 / 1.43 s | 3.7 s |
| 14 | 43,875 (18,844) | 103, proven by bounds | 2,412 | 0.89 s | 5.09 s (3.41 s) | 3.41 s | 0.64 / 1.27 s | 4.2 s |

* The worst observed first request is about 6 s (14 teams, stage A plus practical).
* The search is cached per database file version, anchor, shapes and positions, as in V2.
* Python heap peak (tracemalloc): 17 / 20 / 23 MB; process max RSS 94 / 123 / 133 MB.

Manual evaluations, both sides, including replacement context and IR sensitivity:

| Shape | Time | Forced drops | Drop sets | Lineup solves |
|---|---|---|---|---|
| 3-for-3 | 0.10–0.16 s | none | – | – |
| 4-for-4 | 0.14–0.16 s | none | – | – |
| 6-for-6 | 0.18–0.20 s | none | – | – |
| 2-for-6 | 0.23–0.27 s | 4 | 5,985 | 56 |
| 1-for-6 / 6-for-1 | 0.23–0.31 s | 5 | 26,334 | 24–78 |

## 13. Limitations and real-league validation

* Asset balance thresholds are heuristic and uncalibrated. PAR uses ESPN projections and today's free-agent pool,
  which changes. The streaming baseline is the best free agent each week (optimistic for any one roster spot).
* The beam is not exhaustive and its parameters (6 seeds, width 6, pools of 4 + 4) are fixed. Larger shapes are
  searched only with one other team. 3-for-3 and anything bigger are opt-in or manual-only.
* Needs are lineup-based ranks. They are not a manager's stated preference and not a willingness to trade.
* Injury eligibility depends on nflverse weekly rosters and the code mapping. Without them, an ESPN `INJURY_RESERVE`
  makes eligibility unknown: nothing is excluded, but practical mode treats the trade as speculative.
* Still to validate on the real Los Subies database (none exists in this environment):
  * Etienne's actual stored weekly projections for weeks 6–8, and his `nfl_rosters_weekly` rows and codes;
  * the R01/R48/R04/R05 mapping;
  * how fantasy-IR placements line up with ESPN statuses;
  * free-agent coverage per position (the replacement baselines);
  * position limits in `settingsRaw`;
  * the runtime of the beam and forced drops on real rosters;
  * whether the practical list reads as sensible trades to the league's managers.

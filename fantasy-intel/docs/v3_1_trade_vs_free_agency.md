# V3.1 Trade vs. free agency: replacement-aware opportunity cost

Code:

* `backend/app/analytics/trade_alternatives.py`: the scenarios, the exact one-acquisition search, statements.
* `backend/app/api/routes.py` (`POST /api/trades/evaluate`) and `backend/app/api/trade_discovery.py`
  (`GET /api/trades/discover`): attach `trade_vs_free_agency` to each result.
* `backend/app/analytics/trades.py` (`LeagueState.free_agent_status`) and `trade_data.py`: ESPN's FREEAGENT /
  WAIVERS status is now carried with the free-agent pool.
* Frontend: `frontend/components/TradeVsFreeAgency.tsx`, used by `app/trades/page.tsx` and
  `app/trades/discover/page.tsx`; types in `lib/trade-types.ts`.
* Tests: `backend/tests/test_trade_alternatives.py`. Benchmark: `scripts/benchmark_trade_alternatives.py`.

What did not change:

* The canonical trade value (`trades.evaluate_trade` / `side_lineups`) and every ROS, regular-season and playoff
  delta. The panel recomputes them with the same shared function and reports `consistent_with_canonical`.
* Discovery's search, ordering, modes, tiers and classifications. `trade_practical.py` and `trade_assets.py` are
  untouched (see §8). The K/D/ST default exclusion from automatic discovery stays. The manual analyzer stays
  unrestricted.
* No projection is scaled, no position is penalised, no acceptance probability is produced.

## 1. The question

The canonical delta answers *how many projected starting-lineup points does this trade add or remove?*. A manager
also needs to know *could I get most of that without trading?*. The answer depends on the free-agent pool, so it is
measured against a real, legal acquisition of a currently available player, for both managers independently.

## 2. Root cause of the large D/ST-package gains

Reproduced on the locally synced league (season 2026, evaluation weeks 6–17, 12 teams, 16 active spots, 415
fully projected free agents). The earlier discovery rows came from an older frontend, but the four packages
evaluate the same way on V3:

| Your team sends | You receive | Canonical ROS | D/ST slot | WR slot | TE slot | FLEX | Other |
|---|---|---:|---:|---:|---:|---:|---:|
| Luther Burden III | Chiefs D/ST + A.J. Brown | +103.8 | +65.7 | +37.3 | 0 | +0.8 | 0 |
| Stefon Diggs | Bills D/ST + A.J. Brown | +93.0 | +51.2 | +38.8 | 0 | +2.9 | 0 |
| Saquon Barkley | Chiefs D/ST + A.J. Brown | +81.6 | +65.7 | +38.8 | 0 | −9.9 | RB −13.0 |
| Luther Burden III | Chiefs D/ST + Mark Andrews | +87.2 | +65.7 | −6.6 | +29.4 | −1.3 | 0 |

(Points by starting slot in the optimal lineups over the window, `slot_breakdown`. Every forced drop on your side
cost 0 lineup points: they were bench players.)

Causes:

1. **An empty starting slot.** Your team rosters no D/ST at all: the D/ST slot is empty in all 12 remaining weeks
   (`before_empty_weeks: 12`). Any D/ST fills it, so 51–66 of each gain is a slot fill. That is real lineup value,
   but it does not need a trade: the best available free agent, the Jets D/ST, projects 65.3 over the same weeks
   (the Chiefs 65.7, the Bills 51.2).
2. **A fantasy-IR player counted at full value for the receiver.** A.J. Brown sits in the other team's fantasy IR
   slot (worth 0 to them, IR held as synced) and is eligible but not confirmed back. The receiving side is credited
   with ESPN's projection from week 6: +37–39 at WR. Gemini's classification fix (§8) is the guard for that.
3. **Bench drops and FLEX shuffles** are small (±10) and real.
4. **Incomplete projections** played no part: every involved player is projected in all 12 weeks.

The canonical numbers are correct for what they measure. What was missing is the comparison with free agency.

## 3. Scenarios and metrics

Every scenario uses the same exact weekly optimal lineup (`team_lineups.project_team`) on ESPN projections over the
same window, for each manager independently. `L(X)` = projected starting points of roster X summed over the window.

| Scenario | Roster |
|---|---|
| A, no trade | the current roster (= the canonical "before") |
| B, the trade | the canonical "after" (incoming active, forced drops resolved) |
| C, no trade + one acquisition | current roster + the best available player F − the legal drop it needs |
| E, trade + the same F | the trade and F together, forced drops by the same rule |
| D, trade + its own best player | post-trade roster + its own best available player (diagnostic) |

| Metric | Formula | Meaning |
|---|---|---|
| `canonical_gain` | L(B) − L(A) | the canonical delta, unchanged |
| `free_agent_alternative_gain` | L(C) − L(A) | the best single free-agent move (0 when none improves the lineup) |
| `trade_advantage` | L(E) − L(C) | **what the trade still adds once that move is made** |
| `available_without_trade` | canonical_gain − trade_advantage | the part of the trade's gain the free-agent move already provides |
| `share_available_without_trade` | available_without_trade / canonical_gain | only when canonical_gain > 0 |
| `trade_instead_of_free_agent` | L(B) − L(C) | diagnostic: making the trade instead of the move |
| `trade_plus_best_free_agent_gain` | L(D) − L(A) | diagnostic |
| `trade_advantage_if_both_add` | L(D) − L(C) | diagnostic: each world takes its own best acquisition |

Every metric is reported for ROS, the regular season and the playoffs, plus a known-only value. Missing
projections make the canonical value `None`, never 0.

**Why the headline is L(E) − L(C), not L(B) − L(C).** The free-agent move is open to the manager whether or not the
trade happens. When the trade and F fill the same need, L(E) − L(C) shrinks to what the trade adds beyond F. When
they are independent, it stays the full canonical gain. L(B) − L(C) would instead call every independent trade a
loss whenever a large free pickup exists. On the real league your best single pickup is the Jets D/ST (+65.3, the
empty slot), so every RB/WR trade in discovery would read about −30 to −50. L(B) − L(C) is still shown, labelled
as a diagnostic.

**The only qualitative rule (majority).** "Most of this projected gain is available without trading" means
available_without_trade > canonical_gain / 2. "All of it" means trade_advantage ≤ 0. There are no other
thresholds, multipliers or penalties.

## 4. Modeling rules

* **One acquisition.** A single available player is added now and kept for the whole window. There is no weekly
  churn and no multi-move plan.
* **Only the synced pool.**
  * Players ESPN lists as FREEAGENT form the headline alternative (`free_agent`).
  * WAIVERS players are a separate `waiver_claim_alternative`, shown only when better and labelled as not
    guaranteed.
  * A pool synced without the status is `unknown` and labelled.
* **Projections.** A candidate is scored only when ESPN projects him in every window week he is NFL-eligible.
  * Missing is unknown: such candidates are counted in `excluded_unprojected`, never scored as 0.
  * A projected 0 (bye) is real.
  * Verified NFL-ineligible weeks contribute nothing, exactly as in the lineup engine.
* **Roster legality.** Active capacity and ESPN position limits are enforced with the trade evaluator's own rule,
  `trades.choose_forced_drops`: fewest removals, then the largest remaining total.
  * A candidate who could only be kept by cutting himself is no acquisition (`no_legal_acquisition` when no
    improving candidate is legal).
  * IR players free no active spot.
  * The drop's lineup cost is inside L(C).
* **Displayed drop.** Among drops with equal lineup cost (the rule breaks those ties by player id), the player with
  the fewest projected points is shown, for example a 53-point bench RB rather than a 118-point bench WR. This
  never changes a total.
* **Drops of unprojected players.** A player with no projection is never preferred as a drop. If the rule itself
  drops one, every metric built on that roster loses its canonical value (`drop_cost_unknown`).
* **Independence.** Each manager's alternative is computed separately. When both managers' best alternative is the
  same player, `contested` says only one of them could add him.
* **K/D/ST streaming frontier.** For each K or D/ST position moved in the trade, the best available player at the
  position each week is added for free, before and after the trade. It is labelled an optimistic illustration
  (perfect foresight, no drop, no competition) and is never part of a metric. Separately, the canonical lineups
  themselves now stream K and D/ST at the 3rd-best available player's projection by default
  ([`kdst_streaming_baseline.md`](kdst_streaming_baseline.md)), so the frontier shows only the extra upside of
  always taking the best one.
* **Position alternatives.** For each incoming position, the best available player at that position is shown.
  * A position no flex slot accepts (K, D/ST, QB without OP) compares slot points directly, with the same majority
    rule ("An available D/ST could provide a similar lineup improvement…").
  * A flex-eligible position states only its whole-lineup gain, because its points move between slots.

## 5. Real-league before / after (same four packages)

| Package | Trade | Best free-agent move | Trade beyond that move | Available without trading | Trade instead of the move |
|---|---:|---|---:|---:|---:|
| Burden → Chiefs D/ST + A.J. Brown | +103.8 | Jets D/ST +65.3 (drop a 0-cost bench RB) | +48.6 | 53% | +38.5 |
| Diggs → Bills D/ST + A.J. Brown | +93.0 | Jets D/ST +65.3 | +49.4 | 47% | +27.7 |
| Barkley → Chiefs D/ST + A.J. Brown | +81.6 | Jets D/ST +65.3 | +26.4 | 68% | +16.3 |
| Burden → Chiefs D/ST + Andrews | +87.2 | Jets D/ST +65.3 | +32.0 | 63% | +22.0 |

Before, each row showed only the canonical gain. Now the first and the last two read "Most of this projected gain is
available without trading: adding Jets D/ST gives +65.3…", and the D/ST statement compares the slot directly
(+65.7 by trade vs +65.3 by pickup). For an independent trade such as Breece Hall for DJ Moore + Isaiah Likely
(+31.8), the panel reads "+31.8 of its +31.8 remains after the best free-agent move" and adds "Adding Jets D/ST is
worth +65.3 with or without this trade". The Diggs row stays just under the majority line (47%): the Bills D/ST is
worse than the Jets, and the trade-plus-Jets roster also starts the better of two defenses each week (§9).

Synthetic reproduction (`test_trade_v3.dst_league`, V2 all-positions order): "WR for RB + elite D/ST" rows show
+84 canonical. The D/ST slot is +56, of which the 7-point streamer covers +24, so the trade keeps +60 beyond
free agency: the premium-defense case with no comparable replacement.

## 6. Search, exactness and performance

* **Exact.** The no-drop gain L(R + c) − L(R) bounds a candidate's gain with any drop (the weekly lineup value is
  monotone with non-negative projections). Candidates are solved in decreasing bound order, and the search stops
  once the bound is below the best exact gain.
  * Week-by-week dominated candidates at the same position are skipped (`team_lineups._pareto`, exact).
  * A negative projection voids the bound, and every candidate is solved.
  * Tests compare the result with full brute force (every free agent × every minimal legal drop set), including a
    negative projection and position limits.
* **Batched and cached.** One `FreeAgencyBoard` per request holds the pool and a totals solver (discovery's
  `_Totals`, bit-identical to `project_week`), with memoised roster totals, lineups and acquisitions. Discovery
  shares one board across all shown rows, so each team's no-trade alternative is solved once, not per row.
  Reported numbers always come from `project_team` / `compare`.
* No new ESPN request, no nflverse download, no database write, no migration.

`uv run python ../scripts/benchmark_trade_alternatives.py`: synthetic, 16 active + 1 IR per team, 12 weeks, 400
free agents (10% on waivers), single process.

| Teams | Manual panel (both managers) mean / max | Discovery rows | All discovery panels | Discovery request itself |
|---|---|---|---|---|
| 10 | 0.041 / 0.044 s | 20 | 0.49 s | 3.08 s |
| 12 | 0.052 / 0.089 s | 20 | 0.49 s | 3.38 s |
| 14 | 0.039 / 0.042 s | 19 | 0.48 s | 3.80 s |

On the real synced league: about 0.03–0.04 s per manual evaluation, and the discovery request is about 4 s
including the panels.

## 7. API and UI

* `trade_vs_free_agency` on `POST /api/trades/evaluate` and on every shown `GET /api/trades/discover` row:
  * `available`, `consistent_with_canonical`, `formulas`, `majority_rule`, `contested`, `pool`, `assumptions`,
    `limitations`;
  * per side: `comparison` (all metrics above), `free_agent_alternative` (player, acquisition type, drops, drop
    cost, exact search statistics), `trade_with_free_agent`, `waiver_claim_alternative`,
    `position_alternatives`, `with_trade_alternative`, `slot_breakdown`, `streaming_frontier`, `statements`.
* Discovery puts the anchor's lead statement first in the row's **Concerns** when most of the gain is available
  without trading, when the comparison is unavailable, or when the trade loses. This happens after classification;
  the tier is never read or changed.
* The **Trade vs. free agency** panel shows three numbers per manager: what the trade adds, the best free-agent move
  instead, and the trade beyond that move with its share. The statements follow, with formulas, position
  alternatives, waiver claims, the streaming frontier and assumptions in a collapsed section. The analyzer shows
  both managers; discovery cards show yours.

## 8. Integration with Gemini's classification fixes

Gemini is fixing two PR #18 Codex findings in `trade_practical.py` / `trade_assets.py`: symmetric speculative
classification when either manager receives a fantasy-IR player, and tie-aware starter-caliber ranks. This PR
does not touch either file, does not read `tier` or the starter-caliber flags, and adds its discovery evidence
after `practical.discover` returns. The two merge independently.

After both are merged, the four A.J. Brown packages in §2 are expected to move to the speculative tier
(he is received from a fantasy-IR slot), and this panel will still show how much of the remainder a free agent
covers.

## 9. Limitations

* **Snapshot.** The free-agent pool is today's sync. Other managers can add players at any time, and the waiver
  outcome is unknown.
* **One acquisition only.** Real managers can make several moves, for example adding both a D/ST and a TE.
* **Two players at a one-slot position.** When the trade and the free-agent move leave two players at a one-slot
  position (two D/STs), the lineup engine starts the better one each week. That weekly choice is part of L under
  the engine's "current roster all season" assumption, and it is credited to the scenario that holds both. It is
  why trade_advantage (+48.6) can exceed trade_instead_of_free_agent (+38.5) in the first row of §5.
* **Drops are valued only by lineup cost.** A dropped bench player's value as a trade asset is not counted.
* **Not modeled:** ESPN undroppable lists, game locks and transaction timing.
* **The streaming frontier is an upper illustration**, not an achievable plan.

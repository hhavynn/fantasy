# Trade Discovery 2.0: dual value

Trade Discovery should find trades that make football and fantasy sense for **both** managers, not just
trades whose numbers come out positive. A trade is now judged on two things, kept separate:

1. **Asset quality**: what each manager gives up as a player asset, whether or not that player starts.
2. **Lineup benefit**: what the trade does to each manager's optimal rest-of-season starting lineup.

A third layer, **plausibility**, explains how the two fit together: a set of named checks and a verdict.
There is no new trade engine and no new projection. Every number comes from the existing exact search
(`trade_discovery.TradeSearch`), the manual evaluator (`trades.evaluate_trade`), the asset board
(`trade_assets.AssetBoard`) and the free-agency board (`trade_alternatives.FreeAgencyBoard`). Code:
`backend/app/analytics/trade_dual_value.py` (assessment and ranking) and `trade_ratings.py` (the
0–100 ratings adapter).

## The three signals

### A. Asset value (`dual_value.asset`)

The baseline is ESPN **points above replacement (PAR)** per player over the evaluation window. It
uses `trade_assets` unchanged: replacement is the best free agent at the position each week, with the
league-structure level as a fallback. The package label and thresholds are also unchanged (balanced at
≥ 2/3, significant below 40%, multiple strong assets for fewer).

For each traded player the assessment shows:

* PAR (all weeks, regular season and playoffs)
* positional rank against the league's starters at that position (scarcity), and whether he is
  starter-caliber
* how many weeks he starts for his current team (`starts_before`) and for the receiving team
  (`starts_after`), and his role there (starter, part-time or bench)
* `bench_asset`: true when he has positive PAR but would start in fewer than half the weeks for the
  receiver. A strong bench player keeps his asset value as depth, injury cover and trade currency.
  That value is never counted as starting points.

For each package it shows:

* PAR sent each way and the ratio between the packages
* the imbalance level (`none`, `moderate`, `significant`, or `null` when the data is insufficient) and
  which side it favours
* the best asset each side surrenders

The `depth` block shows, for each side:

* net players in or out (`consolidates`, `adds depth` or `even`)
* forced drops valued as assets
* PAR given up in players who were not regular starters
* open roster spots left
* starting slots left empty

**0–100 ratings** are supporting evidence only (`ratings` in a row's evidence; see "Ratings adapter
contract" below). They never enter a label, a verdict or a ranking.

### B. Canonical ROS lineup benefit (`dual_value.lineup`)

These numbers are copied from the manual evaluator's side views (`trade_discovery._side_detail`) and
never recomputed. For you and for them:

* projected starting points before and after the trade, for the rest of season, the regular season and
  the playoffs
* the net delta for each of those spans, plus the average weekly delta
* weeks improved, worsened and unchanged
* `promoted`: incoming players who start, and own bench players who gain starts
* `displaced`: own players who lose starts
* forced drops and their lineup cost
* missing projection coverage (weeks, players)
* the weekly lineup changes (`entered` / `exited` per week)
* `key_change`: the most important change, in one sentence

Only projected starters count toward the lineup delta. Bench points are never added as starting points.

### C. Practical plausibility (`dual_value.plausibility`)

Each trade gets six checks. Each check is `pass`, `concern`, `fail`, `unknown` or `not_applicable` and
carries its reason:

| criterion | pass | concern | fail |
|---|---|---|---|
| `lineup_gain` | both phase deltas > 0 | — | either ≤ 0 (`unknown` when a delta is missing) |
| `asset_balance` | balanced or replacement-level | moderate imbalance | significant imbalance or multiple strong assets for fewer (`unknown` when the data is insufficient) |
| `positional_fit` | each side gets a player who starts ≥ half the weeks | a side's best incoming player starts, but in fewer than half the weeks | a side gets nobody who ever starts |
| `depth_cost` | no forced drop with PAR, no empty slot | a forced drop with PAR > 0 (`unknown` if its PAR is unknown) | a starter-caliber player is forced off, or a starting slot is left empty |
| `free_agent_alternative` | the trade still adds after your best free-agent move | most of the gain is available from free agency (majority rule) | the trade adds nothing after that move (`not_applicable` in a regular-season or playoff view, or when your lineup does not gain) |
| `uncertainty` | no flags | an injury, IR or eligibility flag on a traded player | — (`unknown` when projections are missing) |

### Verdicts

The verdict is set by a fixed rule (`trade_dual_value.verdict`), applied in this order:

1. `incomplete`: the lineup or asset check is `unknown`. These trades are never ranked against
   complete ones.
2. Both lineups improve and the asset check **fails** → `unlikely_mutual`: *"Both starting lineups
   improve, but the asset packages are lopsided: unlikely to be mutually attractive."*
3. Both lineups improve and any other check is a concern, a failure or unknown →
   `sensible_with_concerns`.
4. Both lineups improve and every check passes (or does not apply) → `sensible`.
5. A lineup does not improve and the asset check passes → `fair_no_lineup_gain`: *"Asset-fair
   exchange, but their starting lineup does not improve."*
6. Anything else → `one_sided`.

### Ranking policy

The policy is conservative and stated in full (`RANKING_POLICY`):

1. Sort by verdict, in the order listed above.
2. Within a verdict, sort by **Pareto front** over three axes, each measured on its own. Fronts are
   computed per verdict, so a trade in a later verdict never pushes back one in an earlier verdict:
   * the smaller side's lineup gain, in points
   * the asset-package ratio (0–1, higher means fairer)
   * certainty (minus the number of injury and missing-data flags)

   A trade falls to a later front only when another trade is at least as good on all three axes and
   strictly better on one. The axes are never added, weighted or converted into each other, so
   100-point rating units and fantasy-point deltas are never mixed. A row with an unknown axis has no
   front and sorts last within its verdict. Missing data is never treated as 0.
3. Within a front, sort by the view's own measure (see the lenses below).
4. Ties fall back to the exact search order (`trade_discovery._rank_key`).

No row carries an acceptance probability, a grade or a "chance". Nothing here has been calibrated
against real accepted or rejected offers, so nothing claims to predict acceptance.

## Lenses (discovery modes)

Each lens draws a **clearly labelled candidate pool** from the existing exact search: the proven top K
by a sort the search already supports, plus heuristic larger-shape rows when they are enabled. It then
assesses and ranks that pool. The new ordering is **never** fed into the best-first search, because its
bounds do not cover verdicts or Pareto fronts. `pool_rule`, `coverage.exact`/`coverage.heuristic`
(exhaustive, cap reached, beam) and `excluded` counts are always returned.

| mode | tab label | pool (exact sort, category) | kept | sections (in order) | within-front measure |
|---|---|---|---|---|---|
| `mutual_upgrades` | Best mutual upgrades | top 100 by `min_side` (phase variant), `MUTUAL_PROJECTED_IMPROVEMENT` for ROS | both phase deltas > 0, or unknown | sensible, sensible_with_concerns, unlikely_mutual, incomplete | smaller-side phase gain |
| `fair` | Fairer asset exchanges | top 100 by `min_side`, no category | asset imbalance none/moderate | sensible, sensible_with_concerns, fair_no_lineup_gain | asset ratio |
| `upside` | Maximum upside | unchanged (top K by `my_gain`) | unchanged | `results` (order unchanged; verdict labels added) | — |
| `consolidation` | Consolidation deals | top 100 by `min_side`, `CONSOLIDATION` | consolidating side gains | sensible, sensible_with_concerns, unlikely_mutual, one_sided, incomplete | smaller-side gain |
| `playoff` | Playoff-focused trades | top 100 by `min_side_playoff` | your playoff delta > 0 | sensible, sensible_with_concerns, unlikely_mutual, one_sided, incomplete | smaller-side playoff gain |

`practical`, `mutual` and `all` keep their exact behaviour, rows and order for existing deep links and
callers. Their rows gain `dual_value` as additional evidence. `CONSOLIDATION` now also covers uneven
heuristic shapes (3-for-1, 1-for-3, 3-for-2, 2-for-3). The exact shapes and their bound pruning
(`_may_match`) are unchanged. A league without a fantasy playoff split returns an empty `playoff`
lens with a note.

## API payload

`GET /api/trades/discover*` accepts the new modes. The API default stays `practical`. The page's
default tab is `mutual_upgrades`.

**Summary row** (`/discover/summary`) adds `dual_value_summary`:

```jsonc
{
  "verdict": "unlikely_mutual",
  "verdict_text": "…",
  "main_concern": "…",            // the most important downside or fairness concern, or null
  "front": 0,                      // Pareto front index, or null
  "you_change": "Jeanty (RB) starts 4 of 4 weeks in your lineup, replacing RB 103 (RB) in 4.",
  "them_change": "…",
  "asset": {"label", "text", "ratio", "imbalance", "favours", "you_send_par", "they_send_par"},
  "lineup": {"you":  {"before": {"ros", "regular", "playoff"}, "after": {…}, "delta": {"ros", "regular", "playoff", "known_ros"}, "average_weekly_delta"},
             "them": {…}},
  "checks": [{"criterion", "status"}]
}
```

**Evidence** (`/discover/evidence`) adds the full `dual_value` (the asset, lineup and plausibility
blocks above) and `ratings` (`trade_ratings.evidence`).

**Top level** adds:

* `lens` (`{mode, label, pool_rule}`)
* `ranking_policy`
* `verdicts` (code → text)
* `criteria`
* `projection_source`: `{"used": "espn", "experimental_comparison": null, "note": …}`

`POST /api/trades/evaluate` (the manual analyzer) adds `dual_value` from the same
`trade_dual_value.for_evaluation`, so the analyzer and discovery assess a trade identically.

## Ratings adapter contract (`trade_ratings.py`)

The 0–100 ratings are developed on their own branch (`feat/player-ratings-100`,
`docs/player_ratings_100.md`). This branch does not edit that branch's files. Trade Discovery reads
the ratings only through `trade_ratings.load_ratings(con, season, ids)`, which reads
`player_scores.{opportunity,production,value}_score_100` and `rating_scale_version` **only when those
columns exist**. The adapter's rules:

* Display only. A rating never enters a delta, a label, a verdict or a ranking.
* Never a price. Ratings are never summed, never compared across positions and never compared across
  opportunity bases. `comparable_pairs` only pairs traded players in the same pool.
* Missing is `null`. A missing rating is never 0 and is never converted from the legacy 0–10 scale.
* Versioned. Only `ratings-100.v1` is read; any other version is reported as unsupported.

Before the ratings branch lands, the evidence reports `available: false` with its reason.

## Projection source

ESPN stays the only evaluation source for the canonical trade engine. The independent model
(`ifp-2`) is not wired into `LeagueState` for long horizons, and its long-horizon numbers have not
been validated. Mixing it with ESPN inside one scenario would need a fallback policy that does not
exist yet. So `experimental_comparison` is `null` and the payload says why. Nothing in this change
reads the production model, writes to the database or changes the lineup source.

## Guarantees and the tests that prove them

| guarantee | test |
|---|---|
| analyzer and discovery give identical canonical deltas and an identical assessment | `test_trade_dual_value.py::test_manual_and_discovery_identical*` |
| a mutually beneficial but lopsided deal is flagged `unlikely_mutual` | `…::test_lopsided_mutual_is_unlikely_mutual` |
| a fair-value deal with no lineup gain is flagged `fair_no_lineup_gain` | `…::test_fair_value_without_lineup_gain_is_flagged` |
| a 2-for-1 consolidation handles forced drops (asset-valued) | `…::test_consolidation_forced_drop_depth_cost` |
| strong bench assets keep their asset value | `…::test_bench_asset_retains_value` |
| missing data is never 0 | `…::test_missing_data_is_not_zero` |
| no acceptance probability anywhere | `…::test_no_acceptance_probability` |
| exact search guarantees unchanged | `…::test_exact_search_guarantees_unchanged` |
| a different anchor team works | `…::test_different_team_selection` |
| no change to the production model or lineup source | `…::test_no_implicit_source_change` |

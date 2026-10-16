# Player ratings on a 0–100.0 scale (`ratings-100.v1`)

Opportunity, production and value ratings are published on a versioned 0–100.0 scale with one decimal,
next to the unchanged legacy 0–10 fields. The legacy fields remain the **internal policy contract**: every
waiver, lineup, need, market-signal and claim/wait/ignore rule still reads them, so no decision changes.

## What a rating means

A rating is a **relative, position-relative rank** among players with enough games this season.

* 100.0 is the top of a relative scale. It is **not** 100% certainty, not a probability of performing,
  not projected fantasy points and not a trade-market price.
* Ratings compare players at the same position on the same basis. A WR 80.0 and an RB 80.0 are each the
  80th-percentile player of their own pool; that does not make them equally valuable.
* Opportunity ratings with different bases (`xfp` vs `targets+carries`, or the waiver fallback
  `espn_points`) are ranked in separate pools and must not be compared with each other.

## Fields

| Field (`player_scores`, `/api/players*`) | Scale | Definition |
|---|---|---|
| `opportunity_score_100` | 0–100.0, 1 dp | Mid-rank percentile of recent opportunity/game within (position, `opportunity_basis`) |
| `production_score_100` | 0–100.0, 1 dp | Mid-rank percentile of recent PPR/game within position |
| `value_score_100` | 0–100.0, 1 dp | `clamp(0.75·opp₁₀₀ + 0.25·prod₁₀₀ + 10·trend, 0, 100)` from **unrounded** inputs |
| `rating_scale_version` | string | `"ratings-100.v1"` |
| `opportunity_score`, `production_score`, `value_score` | 0–10 | Legacy, unchanged (internal policy contract) |

| Field (`waiver_recommendations`, `/api/waivers`) | Scale | Definition |
|---|---|---|
| `opportunity_score_100` | 0–100.0, 1 dp | Waiver-adjusted opportunity: `clamp(base₁₀₀ + 10·trend_adj + 10·injury_adj, 0, 100)` |
| `components.player.value_100` | 0–100.0, 1 dp | The candidate's `value_score_100` (or the ESPN-points percentile fallback) |
| `opportunity_score`, `waiver_score`, `components.player.value` | 0–10 | Legacy, unchanged; `waiver_score` stays a 0–10 heuristic |

`null` always means "not computed" (too few games, no usage data, or a database row written before this
version). It is never a zero and never replaced by a converted legacy value.

## Percentile definition

For a pool of `n` values and a player with `below` values strictly lower and `equal` values tied (including
itself):

```
rank₁₀₀ = 100 · (below + 0.5 · equal) / n       (n > 1)
rank₁₀₀ = 50.0                                  (n = 1)
```

This is the same mid-rank definition the legacy `percentile_scores()` used (`10 ·` instead of `100 ·`), so
the meaning is unchanged. Two consequences that are deliberate:

* Nobody is forced to 100.0. In a pool of 40 the top player is `100 · 39.5 / 40 = 98.75 → 98.8`; the bottom
  player is `1.25 → 1.2` (Python rounds exact halves to even). Ties share the average rank.
* A pool of one player sits at the middle (50.0), as before.

The 0–100 value is computed **directly from the unrounded rank** and rounded once, to one decimal, at
storage. It is not `10 × legacy`: the legacy value was already rounded to 0.1 on the 0–10 scale, so it can
only take 101 distinct values, while `rank₁₀₀` resolves every distinct rank in pools up to ~1,000 players.

Example (synthetic pool of 37 distinct values, 22 below the player): legacy `round(10·22.5/37, 1) = 6.1`;
`10 × legacy = 61.0`; new `round(100·22.5/37, 1) = 60.8`.

## Composite value

The legacy formula is kept with the same weights and meaning, scaled linearly:

```
legacy:  value     = clamp(0.75·opp + 0.25·prod + 1.0·trend, 0, 10)
v1:      value_100 = clamp(0.75·opp₁₀₀ + 0.25·prod₁₀₀ + 10.0·trend, 0, 100)
```

`trend` is the composite usage trend in −1…+1, so it moves value by at most one legacy point (10 rating
points). If one of opportunity/production is missing the other stands in for it, exactly as before. No new
term or bonus is introduced. Because `value_100` uses unrounded inputs it can differ from `10 × value` by
up to ~0.6 rating points; the legacy value carries the rounding error, not the new one.

The clamp is the legacy clamp: a top-ranked player with a rising role can reach exactly 100.0 because the
trend term pushes the blend past the top, just as the legacy value reached 10.0. A percentile rating on its
own never reaches 100.0.

## Rounding and determinism

* Stored with Python `round(x, 1)`. The inputs are dictionaries; output is independent of input order.
* The frontend formats with `toFixed(1)` and the suffix `/100`: `87.4/100`, `61.2/100`, `93.8/100`.
* Sorting and comparison use the stored `_100` numbers. A list never mixes 0–10 and 0–100 values in one
  sort key; missing ratings sort last in both directions.

## Backward compatibility and migration

* `percentile_scores()` and `blended_value()` keep their exact signatures, formulas and rounding. New
  functions: `percentile_scores_100()` (unrounded 0–100) and `blended_value_100()` (unrounded 0–100).
* Schema: new nullable columns are added with `ALTER TABLE … ADD COLUMN IF NOT EXISTS` (no version bump,
  no rebuild, no destructive update). Existing rows read `NULL` until `compute_analytics` runs again.
* The read API selects the new columns only when the table has them, so a hosted MotherDuck copy published
  before this change keeps working and returns `null` ratings.
* The frontend shows a missing 0–100 rating as "—" (Not computed on the 0–100 scale). Where only a legacy
  value exists it is labelled explicitly as `8.7/10 (legacy)`, never as `8.7/100`.

## What stays on 0–10 (and why)

These are policy inputs or different quantities, not player ratings:

* `waiver_score`, `roster_fit_score`, CLAIM/WAIT thresholds (`CLAIM_SCORE 6.0`, `WAIT_SCORE 4.5`,
  `CLAIM_MARGIN 0.5`, `CLAIM_MIN_FIT 3.0`), the watchlist rule (`value ≥ 4` and `opportunity ≥ 6`),
  `INJURY_ADJ`, starter values and `team_position_needs.starter_value`.
* `player_context` RB efficiency/blocking tiers (thresholds 6.7/3.4 on 0–10) and manager aggression
  percentiles in `waivers.load_profiles`.
* Need scores (0–1, drawn on ten ticks).

## Research Lab

Research Lab metric percentiles were already published as integers on 0–100 but were derived from the
rounded 0–10 percentile (`round(pct₁₀ · 10)`). They now come from the unrounded rank
(`round(rank₁₀₀)`). They stay integers because they are shown as ordinals ("87th percentile") and drive the
heat buckets (≥90, ≥75, ≥50), which are unchanged.

## Other workstreams

* **Trade Discovery** (`feat/trade-intelligence`) calls `percentile_scores()` and multiplies by 10. That
  keeps working unchanged. To get the finer granularity, switch to `percentile_scores_100()` instead of
  `10 * percentile_scores()`.
* **Rankings** should read `*_score_100` and `rating_scale_version`, sort on those fields only, and label
  them as relative ratings, not projections.

## Before and after on real data

Measured on a copy of the local league database (2026 season, games through week 5). Analytics were run with
`origin/main` and with this branch, then compared table by table:

| Table | Rows | Legacy fields that differ |
|---|---|---|
| `player_scores` | 504 | 0 |
| `waiver_recommendations` (incl. recommendation, waiver score, decision JSON) | 299 | 0 |
| `team_position_needs` | 72 | 0 |

The only text that differs is the opportunity reason line and the drop-candidate sacrifice line, which now print
the 0–100 rating (`Opportunity 95.2/100 at TE`) instead of the legacy one.

| Player | Opportunity 0–10 → 0–100 | Production 0–10 → 0–100 | Value 0–10 → 0–100 |
|---|---|---|---|
| Joe Burrow (QB) | 8.0 → 80.2 | 9.9 → 98.8 | 8.86 → 88.8 |
| Aaron Rodgers (QB) | 4.3 → 43.0 | 8.7 → 87.2 | 5.45 → 54.6 |
| Tony Pollard (RB) | 8.0 → 79.7 | 7.5 → 75.2 | 8.25 → 82.3 |
| Darnell Washington (TE) | 7.7 → 77.0 | 8.3 → 83.0 | 8.33 → 83.3 |
| Luther Burden III (WR) | 9.5 → 94.6 | 8.6 → 85.5 | 8.93 → 88.9 |
| Malik Nabers (WR) | 9.6 → 96.3 | 9.6 → 95.7 | 10.0 → 100.0 (rising role, clamped) |

Granularity across all 504 rows: distinct opportunity values 100 → 349, production 99 → 319. The largest
`|value_100 − 10·value|` is 0.5 and the highest opportunity rating is 99.7, so no player is forced to 100.

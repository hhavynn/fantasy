# V2 trade opportunity discovery

Code: `backend/app/analytics/trade_discovery.py` (pure search), `backend/app/api/trade_discovery.py`
(`GET /api/trades/discover`), the `/trades/discover` page. Tests: `tests/test_trade_discovery.py`
(synthetic leagues: `tests/synthetic_league.py`).

## What it answers

For one anchor team (yours by default, found through `fantasy_teams.is_mine`; any `team_id` can be passed),
discovery searches every hypothetical trade with every other roster and returns the best candidates by:

| sort | meaning |
|---|---|
| `my_gain` | anchor's rest-of-season starting-lineup delta |
| `their_gain` | the other team's rest-of-season delta |
| `combined` | sum of the two deltas |
| `min_side` | the smaller of the two deltas (a mutual trade ranks by its weaker side) |
| `playoff_gain` | anchor's playoff delta |

Shapes, from the anchor's side: `1-for-1`, `2-for-1` (anchor sends two, receives one), `1-for-2`. A
candidate contains only players currently rostered by the two teams involved. 2-for-2 and multi-team
trades are not searched (see limitations).

Orderings describe projected starting-lineup results. They are not acceptance estimates, there is no
trade grade and no fairness threshold.

## The canonical number is the manual evaluator's

A candidate's impact is exactly what `POST /api/trades/evaluate` computes (docs/v2_trade_evaluator.md):
incoming players active (a traded IR player arrives active), forced drops resolved by the least lineup cost
(never an incoming player unless nothing else is legal), position limits, the optimal lineup rebuilt every
remaining week with the league's real slots and FLEX rules, after minus before for each team, missing
projections never 0 (an incomplete week makes the canonical total NULL), current-IR players held inactive,
byes as real 0s, regular season and playoffs separate. Hypothetical free agents never enter a delta. ESPN's
weekly projections are used unmodified. There is no new valuation: the search only decides *which*
candidates to compute exactly.

A candidate **qualifies** when the anchor's canonical rest-of-season delta or playoff delta is positive.
The anchor's canonical delta must exist; if a missing projection makes it NULL the candidate is counted as
`excluded_missing_data`, never treated as 0. The other side's delta may be NULL (shown as incomplete; it sorts
last and never counts as mutual).

## Search

Candidate counts per opponent with rosters of `m` (anchor) and `n` (opponent) players, IR included:
`m*n + C(m,2)*n + m*C(n,2)`. A 12-team league with 17-player rosters has 54,043 candidates.

### Stage A: screening with provable upper bounds

Let `L(X)` be a team's known-projection optimal starting total in one week for roster set `X`. Assigning
players to slots is a maximum-weight independent set in a transversal matroid (players matched to slots they
are eligible for), so `L` is a weighted matroid rank function. With non-negative projections it is:

* monotone: `L(X) <= L(X + p)`;
* submodular: `L(X + a + b) - L(X + a) <= L(X + b) - L(X)`;
* bounded by the player's own value: `L(X + p) <= L(X) + v_p`.

Exact `L` is solved, per week, for both rosters, every one/two-player removal and every 1-for-1 swap. Then, per
week and with `R` the anchor's roster and `Q` the opponent's:

```
1-for-1  anchor  L(R - s + t)                                   (exact before drops)
         other   L(Q - t + s)
2-for-1  anchor  <= min(L(R - s1 + t), L(R - s2 + t), L(R - s1 - s2) + v_t)
         other   <= min(L(Q - t + s1) + L(Q - t + s2) - L(Q - t), L(Q - t + s1) + v_s2, L(Q - t + s2) + v_s1)
1-for-2  anchor  <= min(L(R - s + t1) + L(R - s + t2) - L(R - s), L(R - s + t1) + v_t2, L(R - s + t2) + v_t1)
         other   <= min(L(Q - t1 + s), L(Q - t2 + s), L(Q - t1 - t2) + v_s)
```

Forced drops only remove players from the post-trade roster, so by monotonicity they can only lower these
totals: the bounds hold after drops too. Summing per-week bounds gives rest-of-season, regular-season and
playoff bounds for both teams.

A candidate is skipped only when its bounds prove the anchor cannot gain (rest-of-season bound <= 0 and
playoff bound <= 0), or prove it cannot match a requested category (e.g. `MUTUAL_PROJECTED_IMPROVEMENT` needs
both sides' bounds > 0). If any projection in the window is negative the bounds are not guaranteed, so
`bounds_valid` is false, nothing is screened out and the best-first search never stops on a bound (every
candidate is evaluated up to the cap). The tests verify the proof on every candidate of a
synthetic league: every skipped candidate has no rest-of-season or playoff gain, and every bound is >= the
exact delta.

### Stage B: exact deltas, best-first

The surviving candidates are evaluated exactly in descending order of the bound on the requested sort key.
With valid bounds the search stops as soon as every remaining candidate's displayed sort value is provably
below the K-th result's: a displayed delta is rounded to 0.01, so it can exceed the exact value its bound
applies to by 0.005 (0.015 for `combined`, which rounds a sum of two rounded deltas), and the search stops only
when `bound + that slack` is strictly below the K-th displayed value. No remaining candidate can then tie the
K-th result and win a tie-break, so the returned top K is provably the top K of all candidates matching the
filters (`termination: proven_by_upper_bounds`). When the bounds are invalid (a negative projection), no
bound is used for anything: nothing is screened out, no category is pre-filtered by a bound, the search never
stops on a bound, and every candidate matching the filters is evaluated (`termination:
all_candidates_evaluated`). If the evaluation cap (`max_exact`, 20,000) is reached first, the search stops
(`termination: evaluation_cap`, `exhaustive: false`): the results are the best among the candidates evaluated,
not a proven top list, and the largest unevaluated bound is reported only when the bounds are valid.

Exact deltas reproduce `trades.side_lineups` rule for rule on a faster totals-only solver: the same optimal
picks summed in the same slot order with the same `sum()` (bit-identical totals, so every rounded delta,
average and week count is equal), the same forced-drop choice (`resolve_roster`'s search over legal
removals, keeping the largest known total, ties by lowest ids, incoming players last), the same completeness
and rounding. Leagues with mixed flex types (e.g. RB/WR + WR/TE) use the exact `assign_lineup` search itself.
The tests compare discovery and the evaluator on well over a thousand candidates, including forced drops, IR,
position limits and mixed flex slots.

### Stage C: the manual evaluator for what is shown

Every displayed result is re-run through `trades.evaluate_trade` (full explanation: starter changes,
displaced starters, forced drops and their cost, open roster spots, hypothetical FA fill, IR sensitivity),
and its deltas and forced drops are compared with stage B (`consistent_with_manual_evaluator`; the API
reports `consistency_failures`, expected 0).

### Simpler-trade equivalents

A two-player candidate whose 1-for-1 sub-trade has identical canonical effects for both teams (every aggregate
delta, every weekly delta, completeness and the forced drops: the extra player changes no projected lineup
and no roster move, e.g. a never-starting throw-in that the other side can absorb without a drop) is hidden by
default, but only when that simpler trade is itself eligible for the current results: 1-for-1 is one of the
searched shapes, the simpler trade qualifies, and it belongs to the requested category (the opponent is the
same by construction). So a 2-for-1-only search or a `CONSOLIDATION` search (which a 1-for-1 can never be in)
shows the two-player trade. When an equivalent is hidden, its simpler trade is listed in its place (even if
the evaluation cap stopped before the simpler trade's own turn), never both and never neither.
`include_equivalents=true` shows the hidden trades, each linked to its simpler equivalent. Hidden trades are
counted (`simpler_equivalents_hidden`). This is a display rule on exact results, not screening.

### What the response reports

`candidates_considered`, `screened_out_by_upper_bound`, `survivors`, `matching_filters`, `fully_evaluated`,
`excluded_invalid_roster` (no legal roster within the forced-drop limit), `excluded_missing_data`,
`not_qualified`, `simpler_equivalents_hidden`, `not_evaluated`, `termination`
(`proven_by_upper_bounds`, `all_candidates_evaluated` or `evaluation_cap`) with its explanation,
`bounds_valid`, `exhaustive`, stage timings, and the consistency check. Counts other than
`candidates_considered`/`screened_out_by_upper_bound` describe the candidates examined for the current query
(the best-first search does not evaluate candidates that provably cannot enter the top K).

Ranking is deterministic: sort key descending (missing values last), then combined delta, shape, opponent id,
player ids. Roster order does not matter (tested).

## Categories (sign-based, no thresholds)

| category | rule |
|---|---|
| `MUTUAL_PROJECTED_IMPROVEMENT` | anchor ROS delta > 0 and other ROS delta > 0 |
| `IMPROVES_MINE_COSTS_THEIRS` | anchor > 0, other < 0 |
| `IMPROVES_MINE_THEIRS_UNCHANGED` | anchor > 0, other = 0 |
| `CONSOLIDATION` | two-for-one where the side receiving one player gains (2-for-1: the anchor; 1-for-2: the other team); the details show the other side's displaced starters, never-starting incoming players and forced drops |
| `PLAYOFF_FOCUSED` | anchor playoff delta > 0 and regular-season delta <= 0 |
| `REGULAR_SEASON_FOCUSED` | anchor regular-season delta > 0 and playoff delta <= 0 |

## Manager context (secondary)

For each opponent in the results, the stored manager-scouting evidence (`analytics/manager_scouting.py`,
unchanged): current position need scores, current roster construction vs league average, completed-trade
count with its sample-confidence label (trade preference stays UNKNOWN), and the scouting headlines with
their confidence. When the opponent receives a player at a position whose stored need score is in scouting's
existing high band (>= 0.66), a plain note says so. None of this changes a delta or an ordering, and nothing
estimates acceptance.

## Performance (synthetic leagues, 17-player rosters incl. 1 IR, 12 remaining weeks, 4-core container)

| teams | candidates | screened out | stage A s | top-20 by my_gain s (evaluated) | their_gain | combined | min_side | playoff_gain | per-manager summary s | 20 full evaluations s |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | 34,391 | 15,963 | 0.63 | 0.32 (82) | 0.21 (60) | 0.93 (479) | 0.19 (50) | 0.09 (122) | 0.19 | 0.89 |
| 10 | 44,217 | 21,078 | 0.91 | 0.29 (82) | 0.13 (65) | 0.92 (484) | 0.16 (50) | 0.10 (122) | 0.30 | 0.85 |
| 12 | 54,043 | 25,769 | 0.97 | 0.33 (92) | 0.22 (67) | 1.02 (498) | 0.15 (51) | 0.10 (112) | 0.45 | 0.77 |
| 14 | 63,869 | 30,451 | 1.30 | 0.45 (111) | 0.29 (71) | 1.02 (347) | 0.29 (47) | 0.17 (104) | 0.67 | 0.76 |

All results exact and consistent with the manual evaluator. A first version that evaluated every survivor
with the full evaluator took ~90 s for 5,000 candidates of a 12-team league; the bounds plus best-first search
plus the totals-only solver bring a full page (stage A, results, per-manager summary, 20 full evaluations) to
roughly 2-3 s. The API keeps the last few stage-A searches in memory, keyed by the database file's
modification time, anchor, shapes and evaluation window, so changing the sort or filter reuses them.

## Safeguards

* Read-only: the module never touches the database; the API opens it read-only. Tests check that no table
  (raw ESPN, projections, derived lineup tables) or the database file changes.
* ESPN projections unmodified; player scores, BUY/HOLD/SELL, waiver recommendations, manager classifications,
  the evaluator's calculations and IR handling are unchanged (the evaluator was only refactored to expose
  `side_lineups`, which it calls itself).
* No dependency on the schedule-intelligence (#7) or projection-calibration (#8) proposals.
* The evaluation window is the manual evaluator's (first scoring period not yet kicked off; an uncertain
  window is flagged; no remaining weeks → 409).

## Limitations

V3 ([`v3_trade_intelligence.md`](v3_trade_intelligence.md)) adds practical modes, an asset-balance diagnostic,
the K/D/ST default exclusion, NFL availability, and heuristic larger shapes (2-for-2 through 3-for-3); the
limitations below describe the exact V2 search, which is unchanged.

* 2-for-2 and three-team trades are not searched. 2-for-2 would add `C(m,2)*C(n,2)` candidates per opponent
  (~200k more in a 12-team league); the same bounds extend to it, but it is left out of the MVP.
* Each team's current roster is assumed for every remaining week; future waiver moves are not modeled.
* Rankings use ESPN's projections, which can be wrong; projections are not adjusted by our role/usage context.
* ESPN undroppable lists and trade-review/veto rules are not modeled.
* Counts of qualified trades are for the candidates examined by the current query, not a census of all
  qualifying trades (most of which are lopsided "I get the better player" trades).
* A league whose projections include a negative value disables all bound use; the search then evaluates
  every matching candidate and, in a full 12-team league, reaches the evaluation cap (results are then the
  best among 20,000 exactly evaluated candidates, reported as not exhaustive). A bound that stays valid with
  negative values (bounding on non-negative clamped values plus the negative mass that forced slots can
  absorb) would restore pruning; it is not implemented.

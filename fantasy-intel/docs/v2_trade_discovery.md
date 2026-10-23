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

Exact deltas reproduce `trades.side_lineups` rule for rule on a faster totals-only solver
(`analytics/lineup_totals.py`): the same optimal picks summed in the same slot order with the same `sum()`
(bit-identical totals, so every rounded delta, average and week count is equal), the same forced-drop choice
(`resolve_roster`'s search over legal removals, keeping the largest known total, ties by lowest ids, incoming
players last), the same completeness and rounding. Nested flex types (each flex slot takes every position the slot
before it takes: one flex type, or FLEX then OP) fill slot by slot with the best remaining value; other mixed flex
types (e.g. RB/WR + WR/TE) and, for nested ones, weeks with a negative value use the exact `assign_lineup` search
itself. See "Deep leagues" below for how the solver avoids re-solving.
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

## Deep leagues (FX-17) and the beam's work budget

FX-17 (`tests/compat_fixtures.py`: six teams, 17 starters with 3 FLEX and 1 OP, 10 bench, 3 IR) took ~470 s for one
`GET /api/trades/discover` and ~19 s for the team-value trade preview. A cProfile of the request put ~95% of the time
in forced-drop resolution: a 27-player active roster is full, so nearly every uneven package needs drops, and
`trades.choose_forced_drops` solved a full 15-week lineup for every single-player removal (its lower bounds) and for
every zero-bound drop set (C(10, k) bench sets). Every one of those solves went through `assign_lineup`
(4.6 million calls), because FLEX + OP counted as "mixed flex types".

Exact changes (results unchanged; the search and every choice are the same):

* Nested flex types use the fast fill (above); `lineup._best_flex` skips its matching bound when the first leaf is
  provably optimal (nested slots, no negative value, no player listed twice), which returns the same picks.
* `LineupTotals.removal_total` (RemovalTotal): the forced-drop search only removes players from one roster. A week
  whose lineup a removal leaves alone (every removed player below the lowest value the lineup uses at his position, or
  tying it with enough equal players left) keeps the full roster's value; any other week is re-solved from the full
  roster's sorted values minus the removed ones. Same values, same fill, same `sum()`.
* `choose_forced_drops` takes two optional hooks: `total_without(combo)` (the total without a few players, without
  rebuilding the roster) and `single_bound(p)` (a player's single-removal cost for the lower bounds). The lineup is a
  maximum-weight independent set of a matroid and starters at one position are interchangeable, so one solve per week
  and position gives every starter's single cost (exact up to float rounding, ~1e-13, far inside `DROP_EPS`). Any
  valid lower bound leaves the choice unchanged: a set is left unsolved only when its bound exceeds the best exact
  cost by `DROP_EPS`. Discovery and trade-vs-free-agency use both hooks; the manual evaluator uses `total_without`
  only and keeps its exact singles.
* Each team's current lineup values per week are kept once (`LineupTotals.base`); stage A, the beam's signals, stage
  B's sides and each forced-drop search start from them and edit only the positions a trade touches (`vec_change`).

Then a disclosed safety net, for the heuristic beam only (V3, `v3_trade_intelligence.md` section 8): a deterministic
work budget of 15,000 lineup totals per opponent. Standard leagues stay well below it, so their results are unchanged
(the E2E goldens and the synthetic M1/M8 leagues compare identical); FX-17 reaches it for every opponent and says so
in `coverage.heuristic.work_budget` and its note ("evaluated 657 of the 803 packages it generated"). The exact search
(stages A-C) is never cut by it: its top list stays proven.

Measured on the 4-core container (one request, fresh process, before = f805d5e; two runs where given):

| request | before | after | after, budget off |
|---|---|---|---|
| FX-17, discover, team 1 | 465.8 / 469.6 s | 12.6 s | 22.2 s (response identical to before) |
| FX-17, discover, team 2 | 285.2 s | 10.2 s | 14.7 s (identical) |
| FX-17, team value (trade preview) | 19.9 / 18.3 s | 2.4 s | same |
| FX-17, team value, `trades=false` | 0.55 s | 0.51 s | same |
| M1 standard, discover, team 1 / team 2 | 6.0 / 5.0 s | 2.5 / 2.3 s | budget not reached |
| M8 IDP, discover, team 1 / team 2 | 6.8 / 6.4 s | 3.6 / 2.6 s | budget not reached |
| E2E golden league, discover, team 1 / team 2 | 1.4 / 1.8 s | 1.2 / 1.5 s | budget not reached |
| 12-team FX-17-shaped league, discover, team 1 / team 2 | not run | 25.2 / 17.5 s | not run |

Stages A and B are exact and grow with the number of opponents; that is what is left in the 12-team deep league.

## Targeted discovery (issue #182)

A manager can ask for an RB upgrade, a named player or bye-week coverage. These are query-driven goals on the same
engine, not another engine and not a new valuation. Every discovery endpoint (`/discover`, `/discover/context`,
`/discover/summary`, `/discover/evidence`, all also under `/v2/leagues/{league_id}` behind `league_scope`) takes:

| parameter | meaning | bounds |
|---|---|---|
| `target_player` | receive this player | integer >= 1; must be on another team's roster in the scoped league, else 422. With no `opponent`, his team becomes the partner; a different `opponent` is a 422 |
| `target_position` | receive at least one player at this position | `QB`, `RB`, `WR`, `TE`, `K`, `D/ST` (422 otherwise; at most 4 characters). A K or D/ST target is tradeable even without `include_kdst` |
| `need` | a roster need resolved by the backend | `weakest`: your lowest-ranked starting position strictly below the league median of projected points per slot (`trade_assets.league_needs`), used as `target_position` (422 together with `target_position`; with `target_player`, a 422 when his position is not the resolved one). `bye`: the first evaluation week the bye planner (`bye-planner.v1`) marks short, a bye cluster or with a current starter on bye, used as `weeks` (targeted mode only; 422 together with `weeks`). An unresolved need is reported with its reason and applies no filter |
| `weeks` | the horizon: week `N` or weeks `A-B` | targeted mode only (422 elsewhere); every week inside the evaluation window; at most 7 characters |

Without any of them every response is what it was, apart from new echo fields (`query.target_player`,
`query.target_position`, `query.need`, `query.weeks`, `target`, `target_positions`, `needs`, `horizon`) that are null
or descriptive. Saved links and the existing modes are unchanged.

**How a target narrows the search.** A `TradeTarget` (player and/or position received) filters the candidates every
stage ranks. Each candidate keeps its own valid upper bound, so the best-first stop still proves the top K among the
matching candidates (tested against brute force on every candidate of a synthetic league). A two-player trade is
replaced by its simpler 1-for-1 equivalent only when that sub-trade still receives the target. The beam for larger
shapes offers the matching players in its pools and seeds only matching 1-for-1 pairs; it only adds players, so every
package it grows still matches. Its results are cached per target. A target filters every mode, and a lens tab
other than the targeted one links without it.

**Focus weeks.** `weeks` (or a resolved `need=bye`) gives the search focus weeks. Each side gets a `focus_delta`: the
same canonical weekly deltas summed over those weeks, NULL when any of them is incomplete. Stage A gets a focus bound
(a sum of the same weekly bounds, so it is an upper bound). The internal sort `focus_gain` uses it, and a candidate
also qualifies when its focus delta is positive. Missing data is judged over the focus weeks: an unknown
rest-of-season total (a missing projection outside them) does not exclude a candidate whose focus delta is complete.
That row's rest-of-season, combined and smaller-side values stay unknown and say so. The stage-A and need cache keys
include the focus weeks and the K/D/ST streaming baseline. The public `sort` values are unchanged.

**The targeted mode** (`mode=targeted`, lens "Targeted search"). The horizon is one of:

* the focus weeks;
* the regular season, searched as focus weeks (`trade_practical.regular_season_weeks`), so a trade whose only gain is
  in the regular season is screened, bounded and qualified like any other, and the label stays "regular-season";
* the playoffs (`playoff_gain`, already screened and qualified);
* the rest of the season (`my_gain`).

The pool is the exact top 100 by your gain over that horizon among matching trades, plus the heuristic rows.
Rows whose horizon delta is known and not positive are counted as `no_gain_in_horizon`; unknown deltas stay in the
view as incomplete. Like the other lenses, it ranks by verdict, then Pareto front within the verdict, then your
horizon gain, then the exact search order, with every verdict shown in its own section. When no regular-season or
playoff week remains (or the league has no split), the targeted mode uses the rest of the season and says so
(`lens_note`).

**Trade-offs (`target_fit`).** Every row of a targeted view, and of any view with a target or focus weeks, states in
plain words:

* the horizon against the rest of the season, called short-term when the gain does not last;
* how each incoming target would be used (starts out of the horizon's weeks);
* the depth given up (outgoing players who start at most half the remaining weeks) and any net loss of roster spots;
* roster fit: forced drops and starting slots the trade leaves empty;
* the partner's change over the horizon.

Every number is a canonical delta or a count from the manual evaluator's lineups. Nothing predicts acceptance or
calls a trade guaranteed (tested).

**Reproducible analyzer link.** Every row carries `manual_evaluator_link` (`ta`, `tb`, `a`, `b`). The page's
"Analyze" link opens exactly that trade in the analyzer (`/trades?ta=&tb=&a=&b=`), and evaluating it gives the row's
deltas (tested, and checked live as `consistent_with_manual_evaluator`).

**`GET /api/trades/discover/targets`** (and `/v2/leagues/{league_id}/api/trades/discover/targets`) runs no search. It
lists every player on another roster in the league (team, position, IR or taxi), the positions the league starts, and
how both needs resolve right now. Resolved needs are cached per data version, league, team and need. The page's goal
form reads it in the targeted lens.

**Caches and isolation.** The view cache key includes every target parameter and its resolution, so a row's evidence
is only served for the view that ranked it (another target's view answers 404 for that key). Under `/v2` every read
goes through the league-scoped connection, so a player id from another league is not on any roster here (422). The
golden suite records three targeted queries, which the league-scope parity and colliding-ids tests replay through
`/v2`.

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

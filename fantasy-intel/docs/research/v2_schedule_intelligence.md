# V2 Schedule Intelligence — Evidence-Aware Research & Engineering Specification

**Status:** Reviewed design guidance, NOT a validated predictive model.  
**Review date:** 2026-10-14. This replaces the speculative coefficients and overconfident statistical claims in the original Gemini draft.  
**Scope:** free, in-season, position-aware NFL defensive data; Los Subies weekly, regular-season and playoff matchup context.

## 1. Decision and governing constraints

Build a transparent position-specific **matchup context** feature, not a new points engine. ESPN weekly projections remain unmodified and are the exclusive point inputs to the exact weekly lineup/trade evaluator merged in PR #9. Do not change opportunity scores, BUY/HOLD/SELL signals, waiver ranks, manager scouting or trade deltas based on unvalidated defensive heuristics.

The initial draft correctly identified promising data and modeling themes but **did not establish** that its numerical coefficients or claimed statistical correlations are predictive. This revision explicitly withdraws its prescribed ridge penalty (lambda=15), pseudo-sample sizes, shrinkage multipliers (0.40/0.70), confidence thresholds (100/150/240 dropbacks), defensive rating-to-points multipliers, and contender/bubble playoff weights (75/25 and 15/85). They are **hypotheses to test**, not vetted engineering requirements.

Use NULL / insufficient evidence for missing data, never zero or neutral as a substitute for unknown.

## 2. Free-source audit (verified loader capabilities, not observed 2026 field completeness)

| Source | Verified documentation | Engineering caveat |
|---|---|---|
| nflreadpy load_pbp | nflverse NFL play-by-play with defensive and EPA-related inputs | Confirm actual 2026 columns, valid plays, non-null coverage and update timing in local ingestion before claiming a computed statistic |
| nflreadpy load_schedules | Regular-season game/week/team schedule; gameday and gametime | Official dictionary defines gametime as **Eastern time regardless of venue**; handle DST correctly; filter game_type=REG |
| nflreadpy load_player_stats / weekly rosters | Weekly player/team identities and statistics | Historical target attribution must use **that week's** reliable GSIS/player-team mapping, not today's team or a name join |
| nflreadpy load_pfr_advstats(stat_type="def") | A weekly PFR advanced defensive-statistics loader exists | It does NOT, by itself, verify team-level YBC allowed or a trustworthy defense-level pressure statistic. Inspect actual returned fields, grain and permissions; omit unavailable fields |
| nflreadpy load_ftn_charting | FTN charting, including documented n_defense_box | Join on real play/game keys, verify coverage and attribution/redistribution conditions; do not extrapolate missing charted plays |
| nflreadpy load_participation | Historical player participation; FTN coverage for newer seasons | **Not available during the current season**; nflverse explains participation from 2023 onward arrives after all postseason games are complete |
| nflreadpy Next Gen Stats | Weekly player-level metrics where published/qualified | Missing qualified-player observations are unknown, not zero; does not establish proprietary route/separation/coverage metrics |
| ESPN weekly forecasts | Existing app stores per-week league-scoring projections | Keep exact source values/provenance, missing-vs-zero distinction and history; this research makes no changes to ESPN forecasts |

Source references independently checked:
- [nflreadpy load-function reference](https://nflreadpy.nflverse.com/api/load_functions/)
- [nflverse availability and update schedule](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html)
- [NFL schedule dictionary](https://nflreadr.nflverse.com/articles/dictionary_schedules.html)
- [FTN charting dictionary](https://nflreadr.nflverse.com/articles/dictionary_ftn_charting.html)
- [Participation dictionary](https://nflreadr.nflverse.com/articles/dictionary_participation.html)
- [Participation loader current-season guard](https://github.com/nflverse/nflreadpy/blob/main/src/nflreadpy/load_participation.py)
- [PFR advanced-stats loader source](https://github.com/nflverse/nflreadpy/blob/main/src/nflreadpy/load_pfr_advstats.py)
- [FTN source/attribution note](https://nflreadr.nflverse.com/reference/load_ftn_charting.html)

**Not verified by these sources:** the original draft's claimed numerical out-of-sample variance explained, predictive correlations, offense/defense variance percentages, exact sample sizes for stabilization, % changes in completion probability, or specific numerical player-point effects. These claims have been removed rather than repeated without a reproducible study.

## 3. Position-specific candidate metrics

Every candidate must show numerator, denominator, period, opponent/sample counts, source timestamp, position identity coverage and missing reasons. A defensive matchup does NOT directly establish an individual player's share of fantasy volume.

| Position | MVP descriptive candidates (supported by PBP if fields pass audit) | Conditional or secondary | Reject as unsupported in-season |
|---|---|---|---|
| QB | Defense dropback EPA allowed per valid dropback; positive-EPA dropback success share | QB scramble/design-run vulnerability, sack or hit-or-sack incidence with small-sample warning | man/zone coverage grades; exact QB fantasy point multipliers |
| RB | Designed-rush EPA allowed and positive-EPA success rate allowed | rush stuffs, RB target share conceded via historically mapped receivers; FTN defenders in box if join coverage holds | undocumented team YBC allowed; unverified offensive line win-rate feeds |
| WR | Dropback EPA/success allowed; WR target share conceded where weekly IDs are complete | explosive pass frequency as volatile ceiling context, not a reliable predictor | slot/outside alignment and CB shadow matchups inferred from middle-field pass location |
| TE | Overall pass-defense efficiency; TE targets/target share conceded with valid historical position mapping | middle-of-field passing as a *location* descriptor, explicitly not TE coverage | "TE funnel" claimed causal from tiny observed samples; scheme-specific coverage labels |

Working definitions, subject to a field-grain audit:
- Dropbacks: eligible REG PBP rows with qb_dropback true, known defteam and finite EPA. Handle sacks, scrambles, penalties and two-point/non-play exclusions consistently.
- Passing success allowed: positive EPA among eligible dropbacks with known EPA; alternatively a verified success field, with its exact definition recorded.
- Designed rushes: rush attempts excluding scrambles, kneels, two-point and other excluded play types. Keep zero/negative yards as actual observations.
- Rush success allowed: positive-EPA eligible designed runs / all eligible designed runs. Do not confuse with a fixed gain-on-down rule.
- Hits/sacks: use an OR of qb_hit and sack rather than summing overlapping events; field behavior must be audited.
- Receiver-position conceded rates: link receiver player IDs to historical weekly rosters; report unmatched fraction. Never substitute current-team or name-based IDs.
- Explosive play thresholds are **descriptive event definitions**, not fitted forecasts. Expose counts and opportunity denominators.

Keep offensive scheme, player opportunity, defensive ability, injuries and game environment conceptually separate. Against-the-defense outcomes may reflect offensive quality and schedule rather than defense alone.

## 4. Shrinkage, recency and opponent adjustment — research questions, not defaults

Start with season-to-date raw rates plus source/sample counts and a clearly labelled league comparison. Compare empirically:

A. Unadjusted full-season-to-date rates with missingness disclosed.  
B. A shrinkage variant for count/proportion rates, with prior strength chosen on **past training periods**, not hand-selected.  
C. Opponent-adjusted defense effects (e.g. regularized offense + defense + season coefficients) with tuned regularization confined to training folds.  
D. A recent-games/recency-weighted alternative only if its out-of-time errors improve materially over the simpler baseline.

EPA means and binary proportions require different statistical treatments. Winsorization, priors and recency windows must all be selected/validated on historical data. Repeated shrinkage should have a documented purpose. Z-scores describe how observed teams compare; they do not prove a stable causal or fantasy-points relationship.

There is currently **no validated basis** to recommend lambda=15, a universal four-week rolling window, prior N=240, a 0.40 shrinkage multiplier, or a cutoff of exactly 100 plays. A display-only data-coverage label is permitted with transparent counts, but must not be misrepresented as measured predictive confidence.

## 5. Weekly, remaining regular season and fantasy playoffs

1. Identify the player's currently known NFL team, opponent and home/away status using the official REG schedule. Explicitly identify bye, unknown team, team changes and missing opponents.
2. Map each scheduled opponent to defensive observations from games already completed before that matchup. No future plays or postgame evidence in historical backtests.
3. Render per-week defensive measurements and counts; explain descriptive direction and uncertainty. A bye is BYE, not an easy matchup or a projected-zero assumption.
4. Derive the fantasy regular-season/playoff cutoff from ESPN league settings and scoring-period mapping, including multi-week playoff matchups. Los Subies currently documents fantasy playoffs as Weeks 15–17, but other leagues must not inherit a hard-coded boundary.
5. Separate next actionable week (as established by the trade evaluator's kickoff-aware evaluation rule), remaining regular-season schedule and fantasy playoffs.
6. For horizon summaries report known/unknown weeks and component descriptions. If computing means, use a consistent scale and document whether bye weeks were excluded. Never turn missing matchups into neutral ratings.
7. Never attach arbitrary **contender** or **bubble-team** playoff percentage weights. Users may choose to filter or sort regular season versus playoffs, but that preference is not evidence of point production.

The first product version should show the actual opponent and why the matchup is described as tougher/easier on a particular observed metric, not one opaque Schedule Difficulty Index.

## 6. Validation gate before any numeric point adjustments

A historical backtest is required before claiming predictive advantage:
- Use genuine pregame ESPN projection snapshots and independently verified completed actuals. The Codex calibration work in PR #8 is a potential measurement tool **once reviewed and integrated**; it does not magically provide historical data.
- At week W, defensive features must use only games and source revisions available before W. Preserve as-of timestamps and actual historical player-team identities, and never leak week W's plays into W's prediction.
- Model 0: baseline ESPN pregame weekly projections. Model 1: baseline plus raw positional fantasy-points allowed as a weak comparison. Model 2: ESPN plus the proposed defensive feature(s). Compare on the **same eligible held-out player-weeks**.
- Use expanding chronological train/validation/test windows, leaving later weeks untouched, not a random player-week split that leaks repeated defenses and players.
- Report overall and QB/RB/WR/TE sample sizes, number of distinct games, MAE, RMSE, signed bias, excluded/ambiguous cases and performance differences relative to ESPN baseline. Use a repeated-game/player aware uncertainty estimate when possible.
- Hyperparameters/thresholds may be tuned only on training/validation samples. A small in-sample p-value or regression coefficient does not demonstrate incremental out-of-time value.
- If data coverage or observed incremental accuracy is insufficient, retain only descriptive schedule context. A negative result is valid research.

**No schedule-derived multiplier, pseudo-confidence value or arbitrary weight enters canonical ESPN projected points or the exact trade evaluator.** Any future projection model requires a separate evidence-backed proposal and explicit independent approval.

## 7. Engineering handoff for Claude

### Phase A: descriptive schedule intelligence (safe first PR)

- Audit local DuckDB 2026 field names, schema/season coverage, available PBP, team IDs, schedule and source freshness. Document absent fields; inspect real data read-only before planning schema.
- Compute only verified per-defense, as-of-week metrics with numerator, denominator, raw value, game count, season, source timestamp and nullable missing reason. No unseen or interpolated route/alignment measurements.
- Resolve player opponents with reliable current player-team IDs and season schedule. Distinguish weekly/byes/regular-season/playoff horizons from league settings.
- Expose a standalone player schedule-context view or endpoint that preserves source attribution, sample sizes, known weeks and unknown weeks.
- Keep this view entirely separate from existing pipeline outputs that feed player scores, waiver recommendations, manager classifications and the exact trade evaluator. New derived persistence, if needed, requires create-only schema migrations and data-preservation tests.
- Validate offense/defense direction, sample denominators, no future-week leakage, schedule/byes, team switches, unknowns, repeatable rebuilds and no effect on existing metrics.
- No hard-coded defensive impact weights or automatic trade-score rerankings.

### Phase B: candidate modeling (separate later PR)

Only after historical data and an out-of-time validation framework exist, compare simple versus shrunk/opponent-adjusted candidate metrics. Select penalties from training data. Report coverage, error bars and gains/losses; do not quietly migrate a speculative formula into the app.

Suggested **conceptual** tables (DDL must follow verified actual field types):
- defense_matchup_weekly_ratings: keyed by season, as_of_week, defteam; include source, games, numerator/denominator and nullable raw descriptive metrics, not invented confidence.
- player_schedule_difficulty: keyed by league/season/as_of_week/player/week to preserve real weekly opponents and league-specific playoff windows. Preserve ESPN and GSIS IDs separately if both are used.

## 8. Approaches to avoid

- Raw fantasy-points-allowed as the sole defensive quality metric; it mixes defense with opponent game plans, pace and offensive talent.
- In-season route rates, targets per route, separation, man/zone and wide/slot assignments when unavailable in verified free feeds.
- Inferring true WR slot alignment from middle-of-field passing.
- Claiming PFR defense data inherently provides team-level yards-before-contact allowed or pressure rates without checking returned columns/grain.
- Calling a weakly supported defensive index predictive, applying an arbitrary ESPN point multiplier, or using fantasy standings to invent trade acceptance percentages.
- Imputing NULL as 0, 1.0, "neutral", or "LOW risk" without actually measuring it.

**Acceptance rule:** every displayed metric is traceable to a supported free source, the exact play/ID filters and sample counts; every unsupported claim is clearly labelled; regular-season/playoff windows match the league; ESPN projections and canonical trade results remain unchanged.

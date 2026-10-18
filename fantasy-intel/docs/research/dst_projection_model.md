# D/ST projection model vs ESPN (dfp-r1, research only)

Date: 2026-10-17. Code:

* `backend/app/analytics/dst_research.py`: scoring, ESPN payloads, game table, ratings, stack, metrics.
* `scripts/dst_research.py`: `fetch`, `reconcile`, `evaluate`.
* Tests: `backend/tests/test_dst_research.py`.

Data:

* ESPN's public read API: weekly D/ST actual and projected raw statistics, every team-week 2019-2025, plus
  2026 weeks 1-5 (one request per season, cached under `data/raw/espn_dst`, git-ignored).
* The local nflverse history DB: schedules with closing lines, and team weeks (QB hits + sacks).
* The live DB: 2026 schedule, the league's scoring items, and the ESPN points used for reconciliation.

The scoring fingerprint is `dst:79fb2f9284165708` (the owner league's slot-16 rules).

**Answer: adopt as a shadow challenger for the one-week projection only. Do not replace ESPN yet, and do not
use it for multi-week horizons.**

On the held-out 2023-25 test seasons (1,632 team-games), the market-informed model beats ESPN's final weekly
D/ST projection, rescored under the league's own rules, on every pre-registered criterion:

| test 2023-25, common support | MAE | bias (actual − pred) | within-week Spearman | pair order acc. |
|---|---|---|---|---|
| **model (market, horizon 1)** | **4.762** | **+0.03** | **0.374** | **0.637** |
| ESPN (final weekly projection) | 4.878 | +0.76 | 0.342 | 0.627 |
| model, ratings only (no line) | 4.925 | +0.04 | 0.304 | 0.612 |
| naive: opponent's D/ST points allowed | 4.932 | −0.05 | 0.266 | 0.598 |
| naive: team's trailing D/ST points | 5.114 | −0.07 | 0.112 | 0.543 |

Against ESPN (week-block bootstrap, 90% CIs):

* MAE: −0.116 [−0.251, −0.014].
* Within-week Spearman: +0.033 [+0.014, +0.052].

The gain is consistent across splits:

* Validation 2021-22: −0.090 MAE, +0.036 Spearman.
* 2026 weeks 1-5 (prospective, 160 games): −0.147 MAE [−0.245, −0.050], +0.081 Spearman.
* By season, it wins 2023 and 2025 on MAE and every season on ranking. It loses 2024 MAE by 0.02.

**What the edge actually is.** Almost all of it is the betting line:

* The model's projection correlates −0.98 with the opponent's implied team total. ESPN's correlates −0.73.
* A stack that uses the line and league means, with no team ratings at all, gets test MAE 4.774 and
  Spearman 0.369. The full model gets 4.762 and 0.374.
* So ESPN under-uses the market, and it under-projects D/STs by 0.76 points a game.

The opponent-adjusted sack, turnover and points-allowed ratings add little on top of the line. Without a line
they are about as accurate as ESPN on MAE and worse at ranking.

**Streaming picks: modestly better ranking, not better picks.** In the pool a streamer actually chooses from
(each week's D/STs outside ESPN's top 8):

* Spearman is +0.029 [+0.001, +0.056] better than ESPN.
* The model's top-3 picks scored 7.86 a week, ESPN's 7.84. Its top-5 picks scored 7.52, ESPN's 7.62.

Outcomes this noisy (actual SD 6.5 points; TDs are 21% of the variance) leave little room for a pick-level
edge. The prospective 2026 weeks are better (top-3 7.93 vs 6.73), but five weeks prove nothing.

**Multi-week: not supported.** Lines exist only for the coming week, so horizons 2-4 use the ratings-only
model. It degrades slowly (MAE 4.925 → 4.958, Spearman 0.304 → 0.288 from horizon 1 to 4), but at every
horizon it is worse than ESPN's final weekly projection.

ESPN's own multi-week projections are not in its history API, so a like-for-like multi-week comparison has to
be prospective (see "Follow-ups").

## 1. Scoring: the league's rules, verified

A D/ST's week is a dict of ESPN raw statistics. The league scores it with every item's slot-16 value:
`pointsOverrides["16"]` when present, else `points`, the same rule `kicker_scoring.py` uses for slot 17.

* Items for statistics a D/ST never accrues are ignored: 19 player items in this league, e.g. passing yards
  and FG 50-59.
* A non-zero item on a D/ST statistic the model does not project makes the module abstain. Examples:
  tackles, return yards, and the alternative 187-196 points-allowed family. The owner league has none.

The owner league scores:

* Points allowed tiers: 0 → +5, 1-6 → +4, 7-13 → +3, 14-17 → +1, 18-27 → 0, 28-34 → −1, 35-45 → −3,
  46+ → −5.
* Yards allowed tiers: <100 → +5, 100-199 → +3, 200-299 → +2, 300-349 → 0, 350-399 → −1, 400-449 → −3,
  450-499 → −5, 500-549 → −6, 550+ → −7.
* Sack +1. INT, fumble recovery, blocked kick and safety +2.
* Every D/ST TD (INT, fumble, blocked kick, kick return, punt return) +6. Defensive 2-pt return +2,
  1-pt safety +1.

`reconcile` (exact to 0.05):

| check | result |
|---|---|
| ESPN 2026 actual D/ST points vs the league score of ESPN's actual raw stats | 160 / 160 exact |
| ESPN's stored league-scored projections vs the league score of ESPN's projected raw stats | 432 / 432 exact |
| latest raw ESPN capture vs the latest pregame snapshot (`espn_projection_history`) | 354 / 354 exact |
| our tier mapping vs ESPN's actual tier indicators, every game 2019-2026 | 3,902 / 3,902 |

So the labels are exactly what the league would have scored. ESPN's baseline is ESPN's own projection under
the league's rules, not ESPN's default-league number.

Two details matter:

* **Tiers are indicators.** A projection needs P(tier), not the tier of the expected points allowed. ESPN
  projects the tiers as probabilities, and the model does the same (section 2).
* **ESPN's points allowed (stat 120) is not the opponent's final score.** It excludes points scored by the
  opponent's defense or special teams: for 2024 home D/STs, 243 of 272 games match the scoreboard, and
  the rest are 2, 4 or 6 lower.

## 2. Model

The model projects each defense D against offense O in one game.

**Ratings.** Each component X has:

    rating_X(D, O) = mu_X · def_X(D) · off_X(O) · home_X

The components are sacks, interceptions, fumble recoveries, blocked kicks, safeties, return TDs, points
allowed, yards allowed, and QB hits + sacks (a pressure proxy, since PFR pressures exist only from 2026).

`def_X` and `off_X` are multiplicative ratings:

* Built from games strictly before the information cutoff.
* Recency-weighted with a half-life in weeks. Each offseason crossed counts as extra weeks of age.
* Shrunk toward the league with `k` pseudo-games, and 4·k for the rare events (blocked kicks, safeties,
  return TDs).
* Opponent-adjusted by alternating passes: a defense's expected count in a past game uses that opponent's
  offense rating.

**Stack.** A linear stack per component maps ratings to E[X]. The market variant adds two inputs: the
opponent's implied team total and the defense's expected margin from the closing line. The stack is fitted
only on earlier seasons' point-in-time features.

**Touchdowns.** Defensive TDs are E[INT] · TD-per-INT, E[FR] · TD-per-FR and E[block] · TD-per-block, at
league rates. Return TDs come from the team's shrunk return-TD rating. P(at least one D/ST TD) is
1 − exp(−λ_TD).

**Tiers.** P(points- and yards-allowed tier) is a kernel-weighted empirical distribution of earlier seasons'
outcomes whose predicted mean was close (bandwidth 2 points or 20 yards). It keeps the lumpiness of NFL scores,
so 13 vs 14 points allowed lands on the correct side of a tier edge. Each predictor (market or ratings-only)
uses its own earlier means.

**Expected points.** E[points] = Σ w · E[count] + Σ w_tier · P(tier). Expectations are linear, so the same
projection scores under any league's rules without refitting.

**Point-in-time rules (tested):**

* A row's features never change when its own week or any later game changes.
* Horizon h uses games through week t − h.
* Tier distributions and TD rates come only from earlier data.
* The stack for season s is fitted on 2020 .. s−1.
* 2019 is burn-in: history only, never a row.

## 3. Protocol and pre-registered adoption criteria

* **Selection on validation 2021-22 only.** A grid of half-life {4, 8, 16} × offseason gap {6, 18} × k
  {3, 6, 12} chose half-life 16, gap 6, k 3. The ESPN blend weight on the model was chosen from
  {0, .25, .5, .75, 1}: the validation MAE minimum is 1.0 (model alone), so the "blend" equals the model.
  The pressure feature was kept only if it improved validation MAE. It did not (4.625 vs 4.617), so it is
  off.
* **Test 2023-25** is reported once with the frozen choices. **Prospective 2026** covers weeks 1-5 plus
  week 6's Thursday game.
* **Comparison** uses common support: every method has a projection on the same team-games.
* **Criteria**, fixed in `scripts/dst_research.py::CRITERIA` before the test was read. On test, the model
  must:
  1. beat ESPN on MAE, with the 90% bootstrap CI upper bound below 0;
  2. not rank worse, with the Spearman-difference CI lower bound above −0.02;
  3. keep |bias| ≤ 0.5;
  4. not be more than 0.25 MAE worse than ESPN prospectively.

| candidate | MAE CI upper < 0 | Spearman CI lower > −0.02 | \|bias\| ≤ 0.5 | prospective | result |
|---|---|---|---|---|---|
| model (market, h1) | −0.014 ✓ | +0.014 ✓ | 0.03 ✓ | −0.147 ✓ | **passes** |
| model (ratings only, h1) | +0.145 ✗ | −0.066 ✗ | 0.04 ✓ | ✓ | fails |

## 4. Results in detail

### 4.1 Ablations (validation and test side by side; nothing chosen on test)

| variant | val MAE | val ρ | test MAE | test ρ |
|---|---|---|---|---|
| frozen (ratings + line) | 4.617 | 0.317 | 4.762 | 0.374 |
| line + league means, no team ratings | 4.650 | 0.305 | 4.774 | 0.369 |
| line only in the stack (ratings off) | 4.640 | 0.305 | 4.771 | 0.368 |
| ratings only (no line) | 4.700 | 0.286 | 4.925 | 0.304 |
| + pressure (QB hits + sacks) | 4.625 | 0.318 | 4.756 | 0.376 |
| no opponent adjustment | 4.618 | 0.313 | 4.760 | 0.376 |

The opponent adjustment and the pressure proxy are neutral on this sample. The line is the signal.

### 4.2 Line timing

nflverse lines are **closing** lines, and ESPN's historical weekly projection is its final stored one (timing
unverified, probably the last pre-lock update). Both are near kickoff. A Tuesday or Wednesday streaming
decision sees earlier lines, which typically move 1-2 points before close.

To test this, the test rows' spread and total were perturbed with N(0, sd) noise. The stack was still trained
on closing lines, and the results average 3 seeds:

| noise sd (points) | MAE − ESPN | Spearman − ESPN |
|---|---|---|
| 0 (closing) | −0.116 | +0.032 |
| 1 | −0.116 | +0.032 |
| 2 | −0.103 | +0.022 |
| 3 | −0.081 | +0.010 |

The MAE edge survives realistic early-line error. The ranking edge shrinks by about two thirds at 3 points of
error. A live version must use the line available at projection time. `team_game_lines` already syncs it,
and each run must record it.

### 4.3 Components (test, vs ESPN's projected component)

| component | model MAE / corr | ESPN MAE / corr |
|---|---|---|
| sacks | 1.399 / 0.235 | 1.408 / 0.189 |
| interceptions | 0.707 / 0.129 | 0.711 / 0.083 |
| fumble recoveries | 0.607 / 0.027 | 0.601 / −0.012 |
| points allowed | 7.09 / 0.413 | 7.52 / 0.301 |
| yards allowed | 61.9 / 0.407 | 69.2 / 0.213 |

* Turnovers are close to unpredictable for both (corr ≤ 0.13). Sacks are slightly predictable.
* Points and yards allowed carry the projection, and that is where the line helps.
* ESPN projects points allowed 0.93 too low and yards allowed 6.8 too high. That is where its +0.76 point
  under-projection comes from.

Tier log loss (lower is better):

* Points allowed: 1.861 (model) vs 2.118 (ESPN).
* Yards allowed: 1.657 vs 2.002. ESPN never projects the <100-yard tier, which costs a little.

Mean predicted tier probabilities match observed frequencies within 0.03 for both.

### 4.4 Touchdown uncertainty (test)

A D/ST scores at least one TD in 12.0% of games, and TD points are 21% of the variance of actual D/ST
points. Neither source can forecast them:

| | Brier | log loss | mean P(any TD) |
|---|---|---|---|
| model | 0.1056 | 0.367 | 0.124 |
| ESPN | 0.1065 | 0.415 | 0.129 |

Both are essentially constant (ESPN 0.10-0.15 for 96% of games). ESPN's worse log loss comes from 32 games
where it projected λ_TD = 0 and 22% of them had a TD.

With TD points removed from both actual and projection, MAE is 4.236 (model) vs 4.363 (ESPN). The non-TD
edge is the same size as the total edge, so the edge is not a TD artifact.

The practical consequence: a D/ST projection is a floor (tiers + sacks + turnovers) plus a near-constant
~0.75-point TD expectation. Any week-level D/ST TD claim is noise.

### 4.5 Intervals

80% bands come from validation residuals, binned by projection quartile. Residuals are right-skewed:
roughly −7 to +8 points around a 5-point projection.

| split | empirical coverage of the 80% band |
|---|---|
| validation | 0.797 |
| test | 0.770 |
| prospective | 0.838 |

Test coverage is slightly under nominal.

### 4.6 Horizons (test; same rows)

| method | MAE | ρ |
|---|---|---|
| model h1 (market) | 4.762 | 0.374 |
| model h1 (ratings only) | 4.925 | 0.304 |
| model h2 (ratings only) | 4.931 | 0.302 |
| model h3 (ratings only) | 4.948 | 0.292 |
| model h4 (ratings only) | 4.958 | 0.288 |
| ESPN (final weekly, ~h0) | 4.878 | 0.342 |

ESPN's number here is its final weekly projection, not a projection made h weeks ahead. ESPN at h ≥ 2 is
unobservable historically. The model's rating-only signal decays slowly with horizon, but without a line it
never reaches ESPN's final weekly projection.

## 5. Caveats

* **Timing.** Closing lines and ESPN's final projection are both late information; see 4.2.
* **Labels and baseline share ESPN's stat definitions.** The labels were verified exact against the league,
  and nflverse is used only for schedules, lines and the pressure proxy.
* **Single league scoring.** The owner league's rules are standard ESPN D/ST tiers. The projection is linear
  in the rules, so other leagues rescore without refitting. A league scoring tackles, return yards or the
  187-196 points-allowed family is refused (the module abstains).
* **Small prospective sample.** 2026 has 5 weeks, so it is consistent with the test but not evidence by
  itself.
* **Common support** is all 32 D/STs every week: ESPN projects every team, so nothing is excluded.

## 6. Recommendation and follow-ups

**Adopt as a shadow challenger** for the one-week D/ST projection, behind the same gates the kicker engine
used:

* parallel storage only;
* nothing in `independent_projection_*`, the production pin, the model registry or `projection_source.weekly()`
  until a prospective record exists;
* status `research_validated_historical`.

Do **not** use it for multi-week (rest-of-season) D/ST planning.

Follow-ups, in order:

1. **Prospective record with real-time lines.** Each week, write a write-once D/ST run that uses the
   `team_game_lines` line at projection time. Score it against ESPN's pregame snapshot
   (`espn_projection_history`, already league-scored) for at least 8 weeks, the repo's shadow-promotion
   minimum.
2. **Multi-week vs ESPN, prospectively.** `espn_projection_history` holds ESPN's league-scored weekly
   projections for weeks 6-17 from each snapshot date. Score them at horizons 1-4 against the ratings-only
   model as weeks complete.
3. **A simpler production candidate.** Line + league means alone gets ~90% of the edge (4.2). If it is ever
   productionised, that is the cheaper, more robust form.

Reproduce:

```bash
cd backend
uv run python ../scripts/dst_research.py fetch
uv run python ../scripts/dst_research.py reconcile
uv run python ../scripts/dst_research.py evaluate
```

`evaluate` writes `data/models/dst/dst_evaluation.json` and `dst_predictions.csv` (git-ignored). It runs in
about 10 seconds.

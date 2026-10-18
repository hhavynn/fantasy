# NFL Savant xFP / xTD vs our nflverse expected points (research only)

Date: 2026-10-18. Data: nflverse play-by-play 2016-2025 and the local history DB (`nfl_ff_opportunity`,
`nfl_weekly_stats`, 2019-2025). Also used: two CSV exports from nflsavant.com/fantasy (PPR, all
players), downloaded by hand with the page's own "Download CSV" button: the 2025 full season (499
players, 67 KB) and 2025 week 10 (275 players). Finally, ifp-2's stored backtest predictions
(`research/context/{confirmation,exploratory}_preds.parquet`). Code: `scripts/savant_xfp_research.py`.
It is offline and read-only, and it prints aggregates. No Savant data is committed, and nothing in
production changes.

**Answer: ignore it.** NFL Savant's xFP is a simple situation-bucket model. We rebuilt it from
nflverse play-by-play, and the rebuild matches Savant's own export at r = 0.9998 (season) and
r = 0.998 (single week). That means we can compute it ourselves for any week or season, so we tested
it ourselves:

* **Our measure is better on its own.** As a single next-week predictor, nflverse xFP beats the
  Savant-style xFP at every position. The difference is significant everywhere (all positions: +0.046
  MAE for Savant, 95% CI [+0.035, +0.056]).
* **Adding it on top of ours does nothing.** On top of recent points + nflverse xFP, the Savant-style
  xFP (equivalently, the Savant − nflverse gap) changes next-week MAE by −0.0002 (CI [−0.004, +0.003]).
* **It does not fix ifp-2's errors.** Used as a correction to ifp-2's backtest predictions, the gap
  helps no more than a plain per-position level shift. In the two out-of-sample directions the gap
  adds −0.001 and −0.004 MAE on a 4.01 baseline, and one of the two CIs includes 0 (§6).

There is nothing to promote and nothing worth a challenger feature. The only interesting by-product
is a calibration fact about our own input (§4): nflverse's QB expected points run about 7% above
realized QB points. ifp-2 is not affected, because it learns its own coefficient on `xfpl`.

## 1. What Savant publishes (glossary, read 2026-10-18)

From nflsavant.com/glossary ("Fantasy opportunity" section), /fantasy and /about:

* **xFP:** the value of a player's opportunities at league-average output for their situation.
  * Each **target** is bucketed by throw depth and field position.
  * Each **carry** is bucketed by down, distance and field position.
  * Each bucket carries the league-average catch rate, yards, TD rate and turnover rate "from a
    decade-plus of play-by-play".
  * A player's xFP is the sum of those bucket averages over every target, carry and pass attempt,
    converted to the chosen format (PPR / half / standard).
* **Scoring:**
  * Yards: 0.1 per rushing or receiving yard, 0.04 per passing yard.
  * Touchdowns: 6 per rushing or receiving TD, 4 per passing TD.
  * Turnovers and conversions: −2 per INT, −2 per fumble lost, +2 per two-point conversion.
  * Receptions: 1, 0.5 or 0 per reception, depending on format.
  * **Sacks and sack fumbles are excluded on both the actual and the expected side.**
* **xTD:** the touchdown slice of the same model, i.e. bucket TD probabilities summed. Passing TDs
  count, which is why QBs top an unfiltered list.
* **FPOE / TDOE:** actual minus expected. Savant's own note on TDOE: across 2019-2025, players
  +3 TDs over expected kept about +1.4 the next year, a year-to-year correlation of about 0.2. This
  fits our existing `td_over_exp` shrinkage toward 0.
* **WOPR:** 1.5 × target share + 0.7 × air-yards share. This is the nflverse definition.
* **Opp/Snap:** (carries + targets) / offensive snaps.
* **Opp:** targets + carries + pass attempts.
* **RZ Opp:** targets + carries inside the 20.
* **Success rate:** the standard 50% / 70% / 100% of yards-to-go on 1st / 2nd / 3rd-4th down. For
  passing, sacks are included.
* **Provenance:** the play-by-play is nflverse (nflreadpy / nflfastR). The bucket edges themselves are
  not published.

### How this differs from what we use (nflverse `nfl_ff_opportunity`)

| | NFL Savant xFP | nflverse ff_opportunity (our `xfp` / `xfpl`) |
|---|---|---|
| Model | Lookup table: league mean per situation bucket | Fitted models per outcome (ffopportunity; gradient-boosted, per its documentation — not re-verified here) |
| Inputs | Target: air yards × yard line. Carry: down × distance × yard line | Richer play context (down, distance, yard line, air yards and more situational variables per the package docs) |
| Outcomes | Catch, yards, TD, turnover (INT; fumble lost) | Completion/reception, yards, TD, INT, two-point conversion, first down. **No fumble model** (no `*_fumble_lost_exp` column) |
| Sacks | Excluded both sides | Not a scoring event in its expected columns. Our actual `league_points` include sack fumbles lost |
| Two-point plays | +2 actual; expected not documented | Explicit `*_two_point_conv_exp` (we include them in `xfpl`) |
| TD modeling | Bucket TD rate (goal-line yards are their own buckets) | Per-play TD probability from the fitted model |
| Scale | Calibrated to league totals by construction (bucket means) | QB total runs ~7% above realized QB points (§4) |
| Grain | Player × chosen window (season or one week) on the site | Player × game, history 2019-2025 + live season |

Both are "expected points from opportunity". Both are built from the same nflverse play-by-play.
They differ only in how finely the situation is described.

## 2. Rebuilding Savant's model, and checking it against their export

`replica()` in the script implements the published description. Because the bucket edges are not
published, ours are:

* **Air yards:** null, <0, 0-4, 5-9, 10-14, 15-19, 20-29, 30+.
* **Yard line:** 1, 2, 3-5, 6-10, 11-20, 21-40, 41-60, 61-80, 81-99.
* **Down:** each down separately.
* **Distance:** 1, 2-3, 4-6, 7-10, 11+.

Scoring is per bucket:

* **Targets:** catch rate, yards, TD and fumble lost, each credited to the receiver.
* **Pass attempts:** yards, TD and INT. Sacks, spikes and two-point tries are excluded.
* **Carries:** yards, TD and fumble lost. Kneels are excluded; scrambles count as carries.

Rates are pooled over 2016-2025 REG. Players were matched by normalized name + position, with team
added when the name is ambiguous (`app.nfl.mapping.normalize_name`): 494/499 season rows and 272/275
week-10 rows matched.

| 2025 agreement (r / mean diff) | season (n = 494) | week 10 (n = 272) |
|---|---|---|
| Savant xFP vs **our replica** | **0.9998** / −0.07 | **0.998** / +0.05 |
| Savant xTD vs our replica | 0.999 / +0.02 | 0.993 / 0.00 |
| Savant xFP vs nflverse xFP | 0.996 / −2.0 | 0.987 / −0.23 |
| Savant xTD vs nflverse xTD | 0.995 / −0.14 | 0.980 / −0.03 |
| Savant FP vs nflverse PPR points | 0.9997 / +0.10 | 0.997 / +0.02 |

The replica *is* Savant's number for practical purposes. Everything below uses the replica, which
lets us test across 2019-2025 every week without further downloads.

**Leakage check.** Bucket rates estimated only from 2016-2022 give the same conclusions:
agreement r 0.9998, every next-week MAE within 0.0012 of the pooled-rate run, and the same §6
results to the third decimal.

## 3. Where Savant and nflverse disagree (2025 season, Savant export vs nflverse)

| Position | n | r | Savant − nflverse, % of nflverse total |
|---|---|---|---|
| QB | 67 | 0.996 | **−7.0%** (≈ −9.7 pts per QB-season) |
| RB | 123 | 0.999 | −0.5% |
| WR | 198 | 0.997 | −0.3% |
| TE | 106 | 0.997 | **−3.6%** |

**Red-zone roles.** Terciles of Savant's red-zone opportunities per game within each position, 2025:

| Position | Red-zone role | xFP gap per game | xTD gap (season) |
|---|---|---|---|
| RB | high | −0.10 | **+0.17** |
| RB | low | +0.01 | +0.01 |
| WR | high | −0.12 | −0.06 |
| WR | low | +0.12 | +0.05 |
| TE | high | **−0.38** | **−0.22** |
| TE | low | −0.05 | +0.03 |

* **RBs with heavy red-zone roles.** Savant's yard-line buckets give goal-line backs slightly *more*
  TD expectation than nflverse does.
* **TEs and WRs with heavy red-zone roles.** Savant gives them *less*.

All of these gaps are a few tenths of a point per game. Week to week the two measures correlate at
0.966 (QB) to 0.993 (RB), and the standard deviation of the 5-game gap is 0.47-0.65 points for
RB/WR/TE and 1.6 for QB.

## 4. Calibration (2019-2025, same player-games; expected ÷ actual PPR)

| | QB | RB | WR | TE |
|---|---|---|---|---|
| nflverse xFP | **1.068** | 1.011 | 1.000 | 0.993 |
| Savant-style replica | 0.983 | 1.007 | 0.997 | 0.956 |

nflverse's expected QB total exceeds realized QB points by about 7%. In 2025, nflverse's expected
passing for QBs comes to 7.23 yards and 0.048 TD per attempt, against a league-wide actual of 7.05
and 0.047 on non-sack attempts. nflverse also counts about 1,000 more QB pass attempts than there
are non-sack attempts in the play-by-play (18,345 vs 17,360). This is most of the QB gap in §3. Savant-style is closer for QBs and about 4% low for TEs.
ifp-2 uses `xfpl` only through fitted coefficients, so a constant scale bias is absorbed. This is a
documentation note, not a defect.

## 5. Does Savant xFP predict next-week points better?

**Setup:**

* **Rows:** every REG player-week 2019-2025 (QB/RB/WR/TE with a box-score row) with at least one
  previous game.
* **Features:** means over the player's previous ≤ 5 games.
* **Fit and test:** OLS per position, fit on 2019-2022 (19,862 rows), test on 2023-2025 (15,617 rows).
* **Target:** PPR points of the week (this league's scoring is full PPR, so `xfpl` ≈ nflverse xFP).
* **CIs:** 95%, from 1,000 bootstrap resamples of whole weeks.

| Test MAE | QB | RB | WR | TE | All |
|---|---|---|---|---|---|
| A: recent points | 6.536 | 4.829 | 5.028 | 3.880 | 4.927 |
| F: nflverse xFP only | 6.495 | 4.809 | 4.966 | 3.810 | 4.878 |
| G: Savant xFP only | 6.630 | 4.830 | 5.015 | 3.827 | 4.923 |
| B: points + nflverse xFP | 6.462 | 4.768 | 4.939 | 3.803 | 4.850 |
| C: points + Savant xFP | 6.500 | 4.772 | 4.959 | 3.813 | 4.867 |
| **D: points + both (= gap)** | 6.462 | 4.773 | 4.935 | 3.802 | 4.850 |
| B2: B + nflverse xTD | 6.458 | 4.765 | 4.922 | 3.799 | 4.841 |
| E: B2 + Savant xFP and xTD | 6.439 | 4.770 | 4.914 | 3.795 | 4.836 |

| Paired difference (MAE, 95% CI) | All | Comment |
|---|---|---|
| G − F (Savant alone vs nflverse alone) | +0.046 [+0.035, +0.056] | nflverse better at every position |
| C − B (swap nflverse for Savant) | +0.016 [+0.011, +0.021] | worse |
| **D − B (add Savant / the gap)** | **−0.0002 [−0.004, +0.003]** | no information |
| E − B2 (add Savant xFP + xTD to nflverse xFP + xTD) | −0.005 [−0.010, −0.001] | see below |

**Why E − B2 is not a signal.**

* **It changes sign by position.** WR gains (−0.008 [−0.014, −0.004]), RB loses (+0.005
  [+0.002, +0.009]), and QB and TE CIs include 0.
* **The gain comes with a negative weight on Savant.** In the WR fit, Savant xFP enters with a
  negative coefficient: it acts as a correction to nflverse rather than as better information.
* **It is one of many comparisons.** Read as a family, the result is noise around zero.

The xTD term that *does* help (B2 vs B: −0.009 overall) is nflverse's own xTD, which ifp-2 already
carries as `xfpl_td_recent`.

## 6. Does the gap explain ifp-2's own errors?

**Setup:**

* **Residual:** ifp-2 backtest residual = label − `pred_ifp2` (confirmation 2023-24: 13,345 rows;
  exploratory 2025: 6,720 rows; 2026 excluded because no 2026 play-by-play was loaded).
* **Gap feature:** Savant-style minus nflverse xFP, averaged over the player's last ≤ 5 games strictly
  before the forecast week. It uses ifp-2's own lookback (current + previous season); a missing gap is
  set to 0.
* **Correction:** a per-position linear correction, fit on one window and scored on the other.
* **Control:** a correction with only an intercept, so that a level shift is not credited to the gap.

| Fit → test | ifp-2 MAE | intercept only | + gap | gap vs intercept (95% CI) |
|---|---|---|---|---|
| 2023-24 → 2025 | 4.0084 | 4.0198 | 4.0186 | −0.001 [−0.007, +0.004] |
| 2025 → 2023-24 | 4.0163 | 3.9979 | 3.9939 | −0.004 [−0.0075, −0.0005] |

* **Correlation.** Correlation of residual with gap, by position: −0.008 (TE) to −0.052 (RB).
* **Sign.** Gap slopes are mostly negative: when Savant-style recently exceeded nflverse, ifp-2
  slightly over-projected. That is the same direction as §5's negative weights, i.e. nflverse's richer
  model is the better-informed of the two.
* **Size.** At most a few thousandths of a point, under 0.1% of MAE, and only one direction excludes
  0. That is below anything the promotion rules would act on. Most of the "improvement" in the table
  is a year-specific level shift (the intercept-only column), not the gap.

## 7. Recommendation

1. **Ignore Savant xFP/xTD as a model input.** Savant is a coarse version of what we already ingest:
   same play-by-play, fewer situational variables. It is measurably worse alone and adds nothing on
   top. Do not add Savant as a dependency, and do not schedule downloads.
2. **Do not build the bucket replica as a challenger feature.** This study already is that challenger
   (§5-6), and it fails. `scripts/savant_xfp_research.py` keeps the replica if anyone wants to revisit
   with different bucket edges. Given r = 0.9998 with Savant, finer edges would just converge toward
   nflverse's model.
3. **Useful by-products.** We do not need to act on them, but they are worth knowing:
   * nflverse QB xFP runs about 7% high in absolute terms (§4). Keep this in mind anywhere `xfp` or
     `xfpl` is *displayed* as an expected score rather than used as a regression feature. The usage
     and research pages already label it an opportunity measure.
   * Savant's TDOE persistence note (about 25% of an extreme TD gap persists) agrees with ifp-2's
     shrinkage of `td_over_exp`. There is no change to make.
   * Savant remains a convenient manual cross-check: player cards, the custom play-by-play explorer,
     WOPR / Opp/Snap boards. Use it for spot-verifying our usage numbers by hand, within the site's
     terms (manual exports only).

## Reproducing

```bash
cd backend
uv run python ../scripts/savant_xfp_research.py \
  --pbp-dir <scratch>/pbp \
  --savant-dir <folder with the two manual Savant CSVs> \
  --history-db ../data/nfl_history.duckdb \
  --preds-dir ../data/models/research/context        # add --rates-through 2022 for the leakage check
```

* **Play-by-play.** Missing seasons are fetched with nflreadpy into `--pbp-dir` (keep it outside the
  repo).
* **Savant exports.** Recreate them by hand at nflsavant.com/fantasy: Year 2025, PPR, All players
  (no minimum), Full season and then Week 10, ↓ CSV. Save the week file as
  `nflsavant-fantasy-ppr-2025-wk10.csv`.

# Kicker projection engine (kfp-1.0.0)

Fantasy Intel's independent weekly kicker (K) projection. It is a separate model with its own identity, storage and
artifact approval, so it can never change, invalidate or relabel the skill-position models (ifp-1.0.0 / ifp-2.0.0).
ESPN projections are never an input.

## 1. Where it sits

| Piece | Module | Notes |
|---|---|---|
| Kicking + drive tables | `app/nfl/kicking_pbp.py` | `nfl_kick_attempts`, `nfl_team_game_drives` from nflverse PBP; written by the live sync, the history sync, and `scripts/sync_kicking_pbp.py` (backfill) |
| League kicker scoring | `app/analytics/kicker_scoring.py` | per-kick points from the ESPN items, `k:` fingerprint, ESPN reconciliation |
| Venues | `app/analytics/kicker_venues.py`, `kicker_data/stadiums.json` | per-game venue (overrides, registry, unknown), roof capability, coordinates, elevation |
| Weather | `app/analytics/kicker_weather.py` | NWS kickoff-window forecasts, write-once `weather_forecasts` snapshots |
| Model | `app/analytics/kicker_model.py` | features, GLMs, conversion curve, empirical-Bayes skill, artifact |
| Selection / evaluation | `app/analytics/kicker_backtest.py` | walk-forward protocol, ablations, frozen `PRODUCTION` config |
| Live runs | `app/analytics/kicker_live.py` | expected kicker, guards, weather, explanations, participation |
| Storage + read side | `app/analytics/kicker_store.py` | `kicker_projection_runs` / `kicker_projection_rows`, hash, gate, `select_run`, `overlay` (SQL + stdlib, hosted-safe) |
| Consumers | `projection_source.weekly` (K overlay), `forward_ours`, `GET /api/projections/kickers`, K rows of `GET /api/projections/independent`, `/kickers` page |
| CLI | `scripts/kicker_projections.py` | `reconcile`, `evaluate`, `fit [--register]`, `project [--week] [--through-week]` |
| Automation | `scripts/learning_cycle.py` stage `kicker` | after `project`; fits the season artifact once, then projects the target week and the rest of the season. A failure is a warning, never a failed cycle |

### Why parallel tables, not `independent_projection_runs`

Several evidence readers (promotion tracker, ESPN benchmark, Start/Sit evaluation) select skill runs by season only,
and a kicker run is made under a different (kicker) scoring fingerprint. Writing kfp rows there would leak kicker
rows into skill-position evidence, and adding row columns would change every stored ifp run's content hash. The kicker
tables reuse the same contract instead: append-only, write-once, `content_sha256` over canonical meta and rows,
verified by every reader, an identical rerun is a no-op, and different content under an existing run_id raises.
Artifacts are approved in the shared write-once `approved_artifacts` table under model_version `kfp-1.0.0`. That gate
filters by model version, so it can never approve an ifp artifact. As with ifp-2.0.0, the gate fails open, marked
"unverified", until the first kfp artifact is registered.

The ifp runs still carry their K rows as `unsupported_position`, exactly as before (their hashes are unchanged).
`projection_source.weekly(..., "independent")` then fills K players that would otherwise show ESPN with the verified
kfp run's value (`ours` / `ours_zero`), and reports it under `kicker_run`. ESPN mode is untouched.

## 2. Scoring

ESPN items are resolved into the points ONE kick earns (`fg_points(distance, outcome)`, `pat_points(outcome)`). Items
are additive exactly as ESPN applies them (an overlapping 50+ and 50-59 item both pay, and the resolver flags it).
The K-slot override (slot 17) wins. "Missed" includes blocked attempts. A rule the resolver does not understand that
pays a K-slot player makes the league UNSUPPORTED, and every row abstains (`unsupported_scoring`). Fake-kick
offensive stats and D/ST events with a base value are listed as `unmodeled`. The fingerprint hashes the effective
per-kick table, so it is independent of item order and of the skill-position fingerprint.

**Los Subies (2026):** FG made 0-39 = 3, 40-49 = 4, 50-59 = 5, 60+ = 6, any FG missed = -1, PAT made = +1 (no PAT
miss penalty). **Reconciliation: 160 / 160 kicker-weeks exact** against ESPN's actual points (weeks 1-6).
Treating a blocked FG as *not* a miss reconciles only 153/160 (7 blocked FGs), which is how the blocked rule was
verified. 10 ESPN K-weeks have no nflverse row (the kicker did not kick); they are counted separately and are 0 on
both sides.

## 3. Model

Point-in-time features for a game in (season S, week W) use only games before W in S plus all of S-1:

* **Team offense** (shrunk per game: current season toward a prior of `prior_k` previous-season games shrunk toward
  the league): FG tries, PAT tries (touchdowns), drives reaching the opponent 35, red-zone TD rate, points, drives,
  FG tries per scoring-range drive.
* **Opponent defense**: the same quantities allowed (an opponent's defense row is its opponents' offense rows).
* **Stadium**: home, fixed roof, retractable roof (position unknown), elevation.
* **Weather (retrospective only)**: nflverse's observed game-time wind and temperature.
* **Game script**: point differential per game of both teams, 4th-down go rate in FG range, 2-pt tries per TD.

```
E[points | he kicks] = lambda_FG * sum_d P(d) [p(d) * made(d) + p_miss(d) * missed(d) + p_blk * blocked(d)]
                     + lambda_PAT * [p_PAT * pat_made + ...]
```

* `lambda_FG`, `lambda_PAT`: ridge-penalized Poisson GLMs (IRLS, numpy).
* `P(d)`: the league's per-yard FG distance histogram split at 50, mixed with the kicker's shrunk 50+ share
  (`long_k` = 25 tries of shrinkage).
* `p(d)`: a league logistic curve, quadratic in distance (smooth across the 39/40 and 49/50 cutoffs), plus venue terms,
  plus the kicker's **empirical-Bayes skill**. That skill is one Newton step of the Gaussian-prior penalized
  likelihood: `u = sum w (y - p) / (sum w p (1 - p) + 1 / tau2)` over all of his earlier FG **and PAT** tries,
  misses and blocks included, weighted `decay^(seasons ago)`. Two makes from 55 yards move him far less than 30 of
  40 (tested). A kicker with no kicks gets `u = 0`, the league average (labeled cold start).
* Non-makes are split into blocked and missed by the league block rate, so a league that scores blocks differently
  is handled.

Also evaluated: a **direct ridge** on points (same features plus kicker point history), and blends of the two.

### Production configuration (chosen on validation seasons only)

| Choice | Value | Evidence (validation 2021-22) |
|---|---|---|
| FG-try volume | league rate (no features) | Poisson deviance 0.942 intercept-only vs 0.943 with offense features: **team and opponent context do not predict FG tries** |
| PAT-try volume | offense + defense + stadium | deviance 1.07 (league rate) -> 0.98 |
| Conversion | league curve + venue + kicker skill (tau2 = 0.1, decay = 0.7) | FG log loss 0.3644 (league curve) -> 0.3600 |
| Observed weather | excluded | conversion log loss worsens (0.3600 -> 0.3625); points MAE gain about 0.01 |
| Game script | excluded | validation MAE +0.0017 (worse); test -0.008: inconsistent |
| Blend with direct ridge | none (structural only) | 0.75 blend 3.5937 vs 3.5931 |
| Shrinkage / ridge | team_k = 2, prior_k = 2, ridge 0.3 | Poisson-deviance grid |

The opportunity finding matters for the brief's "red-zone stall" hypothesis. The model does include FG-range trips
and red-zone TD rate, but after shrinkage they did not reduce held-out FG-try deviance. Weekly FG volume regresses
almost completely to the league rate. The kicker-to-kicker spread comes instead from touchdown volume (PATs), accuracy,
long-range usage and venue.

## 4. Evaluation

Data: nflverse 2019-2025 (history DB) plus 2026 weeks 1-6 (live). 2019 is burn-in only (no previous season for its
priors). Rows are (team-game, the kicker who kicked). Predictions are conditional on him kicking.

| Season | team-games | FG tries | PAT tries | kickers |
|---|---|---|---|---|
| 2019 | 512 | 983 | 1,210 | 42 |
| 2020 | 512 | 960 | 1,338 | 47 |
| 2021 | 544 | 1,027 | 1,257 | 49 |
| 2022 | 542 | 1,062 | 1,187 | 45 |
| 2023 | 544 | 1,060 | 1,169 | 41 |
| 2024 | 544 | 1,115 | 1,241 | 45 |
| 2025 | 544 | 1,088 | 1,262 | 43 |
| 2026 (wk 1-6) | 160 | 327 | 377 | 32 |

Protocol: tune on 2021-2022, each season predicted by models fitted on earlier seasons only. Report 2023-2025 once
with frozen choices. 2026 is the prospective check (models fitted on 2020-2025).

### Held-out test, 2023-2025 (1,603 kicker-games with every baseline defined)

| Model | MAE | RMSE | Bias (actual - pred) |
|---|---|---|---|
| **kfp-1.0.0** | **3.769** | 4.760 | +0.38 |
| Direct ridge | 3.769 | 4.754 | +0.24 |
| A3 team FG/PAT rates x kicker conversion | 3.843 | 4.866 | +0.22 |
| A1 season-to-date average | 4.149 | 5.260 | -0.26 |
| A2 last-4 average | 4.152 | 5.261 | -0.01 |

Paired MAE differences, kfp minus baseline, with a cluster-bootstrap 95% CI over weeks: vs A1 **-0.38 [-0.55, -0.23]**,
vs A2 **-0.38 [-0.48, -0.28]**, vs A3 **-0.07 [-0.12, -0.02]**, vs direct ridge 0.00 [-0.02, 0.02].
Weekly ranking: mean Spearman 0.142 (A1 0.070, A2 0.060), top-12 AUC 0.568, top-12 precision 0.46.
Per season MAE: 2023 3.65, 2024 3.82, 2025 3.81.

Calibration (test): FG tries by quintile are predicted 1.91-1.95 vs actual 1.92-2.05. PAT tries track well
(1.66 -> 2.90 predicted vs 1.64 -> 3.03 actual). FG make probability by distance:

| Distance | n | predicted | actual |
|---|---|---|---|
| 18-29 | 692 | 0.979 | 0.978 |
| 30-39 | 907 | 0.929 | 0.939 |
| 40-49 | 889 | 0.814 | 0.803 |
| 50-54 | 492 | 0.692 | 0.740 |
| 55+ | 283 | 0.596 | 0.608 |

80% interval (bands from validation residuals): **77.0%** empirical coverage on test (slightly under-covered). The
production artifact's bands are refitted on all 2021-2025 out-of-sample residuals.

**Known bias:** the model under-predicts by about 0.38 points per game on test. League kicking keeps improving from
50-54 yards (actual 74% vs 69% predicted), and the curve, fitted on earlier seasons, lags. Recency-weighted training
(`train_decay` 0.8 / 0.6) was tested on validation and did not improve MAE, so it was not adopted. This is the first
follow-up candidate.

Cohorts (test MAE / bias): open-air 3.61 / +0.17; fixed roof 3.96 / +0.45; retractable 4.17 / +1.03 (largest bias);
observed wind >= 15 mph 3.16 / -0.66 (n = 92); observed temperature < 40F 3.55 / -1.00 (n = 150); kickers with < 60
weighted kicks 3.72 / +0.12.

### Ablations (structural model; FG and PAT volume use the same groups here)

| Variant | Validation MAE | Test MAE | Test per season (2023 / 2024 / 2025) |
|---|---|---|---|
| ability only (league volume) | 3.624 | 3.785 | 3.660 / 3.863 / 3.831 |
| + offense | 3.627 | 3.785 | 3.661 / 3.852 / 3.841 |
| + defense | 3.620 | 3.781 | 3.656 / 3.856 / 3.831 |
| + stadium | 3.612 | 3.763 | 3.668 / 3.821 / 3.801 |
| + weather (retrospective, observed) | 3.601 | 3.747 | 3.655 / 3.808 / 3.779 |
| + script (no weather) | 3.621 | 3.755 | 3.666 / 3.793 / 3.804 |
| full + weather (retrospective) | 3.609 | 3.738 | 3.654 / 3.782 / 3.780 |
| **production** (FG: league rate; PAT: off + def + stadium) | 3.593 | **3.769** | 3.655 / 3.819 / 3.811 |

Reading: venue terms help in both splits. Offense and defense help PAT volume, but used for FG volume they add noise,
which is why production splits the two. Observed weather gives a small, consistent points gain (~0.01-0.015) but
worsens the conversion log loss. Being observations, it is an upper bound on what a forecast could deliver, so it is
not used.

### Weather

* Provider: NWS api.weather.gov (US public domain, free for commercial use, no key). Open-Meteo's free tier is
  non-commercial and is not used. No paid API.
* Sampled over kickoff -> kickoff + 3.25 h, from hourly periods plus gridpoint gusts and precipitation, prorated.
  Wind direction is recorded, never modeled: field orientation is not verified.
* Fetched only for open-air US venues, only before kickoff, only within about 6.5 days. Snapshots are append-only,
  reused for 6 h, and served stale up to 36 h when the provider is down (labeled). Otherwise the result is
  "unavailable" and the run continues without it.
* **kfp-1.0.0 does not use weather as an input.** It is stored and shown with its retrieval time and labeled "not a
  model input". The archive of real pregame forecasts is what a future prospective weather test needs. No pregame
  forecast archive exists for past seasons, so the only historical experiment possible is the retrospective one above.

### Venues

`kicker_data/stadiums.json` (versioned, `kicker-venues/v1`) covers every 2019-2026 venue: coordinates, elevation, roof
capability (open_air / fixed / retractable), surface and seasons. It also holds per-game overrides for games nflverse
mislabels. All seven 2025 international games are listed under the home team's own stadium (e.g. Sao Paulo as SoFi
"dome"), and 2026 week 5 (London) is listed as JAX00. The registry also corrects nflverse's 2026 roof labels for
Munich, Paris and Melbourne ("dome" for open stadiums). A retractable roof's game-day position is `retractable_unknown`
pregame. nflverse's open/closed is used only retrospectively.

### ESPN comparison (2026)

ESPN kicker projections in the database: per-week stored values for weeks 1-5, synced at an **unverified time** (not
proven pregame), and pregame snapshots from 2026-10-14 onward (weeks 6+).

* Stored projections, timing unverified (n = 66 matched kicker-weeks): kfp MAE 3.90 vs ESPN 4.12, paired difference
  -0.21 [-0.36, -0.10]. **Not evidence of superiority**, because ESPN's numbers may not be pregame.
* Genuine pregame snapshots: n = 2 completed kicker-weeks so far. Too few to say anything.

Prospective 2026 weeks 1-6 (155 common-support rows): kfp 3.66 vs A1 4.21, A2 4.16, A3 3.83.

## 5. Production run

`kicker_live.build_run` per ESPN kicker:

* **Expected kicker** of each team: the kicker of the team's last completed game, if he is still on the latest NFL
  weekly roster as ACT/INA. Otherwise the single ACT kicker on the latest roster (new signing, flagged). Otherwise the
  depth chart. Otherwise none. A player absent from the newest roster week is off-roster (nflverse drops released
  players). A departed kicker gets `no_nfl_team` or `not_expected_kicker` and never his old team's opportunities.
* Coverage statuses: `projected`, `bye` (0), `withheld_unavailable` (0), `not_expected_kicker`, `no_nfl_team`,
  `ambiguous_kicker`, `uncertain_id_mapping`, `unsupported_scoring`. Only bye and withheld are credible zeros.
  Everything else keeps ESPN's number in the switch.
* `p_play` from an empirical table: P(the previous kicker kicks | still rostered, pregame designation), history
  2020-2025. No designation 0.992 (n = 2,854); Questionable 0.75 raw (n = 59). `expected_points = p_play x
  projected_points`.
* Explanations are counterfactuals of the model itself: points change when touchdown volume, accuracy, long-range share
  or venue is set to the league average. The headline ("Strong / Average / Weak kicking outlook") is derived from the
  projection vs the league median.
* Rest of season: features as of the cutoff week, the future opponent and venue, no weather, flagged experimental.

### Sample projections (2026 week 7, run made 2026-10-16 19:17 UTC, data through week 6 TNF)

Artifact `kfp-1.0.0_2026.json` (trained 2020-2025). Scoring `k:b498324a91e7a6a0` (3/4/5/6, miss -1, PAT +1).

| Kicker | Game | Ours (exp.) | ESPN | FG made / tries | PAT | Why (top terms) |
|---|---|---|---|---|---|---|
| Jake Bates | DET vs GB | 9.0 | 9.2 | 1.7 / 2.0 | 3.1 / 3.2 | touchdown volume +0.9, indoors +0.3, accuracy +0.2 |
| Eddy Pineiro | SF @ ATL | 8.7 | 8.2 | 1.7 / 2.0 | 2.8 / 3.0 | touchdown volume +0.7, retractable roof +0.4 |
| Ka'imi Fairbairn | HOU vs NYG | 8.5 | 11.3 | 1.7 / 2.0 | 2.3 / 2.4 | retractable roof +0.3, accuracy +0.3 (87% at 45 yd) |
| Will Reichard | MIN vs IND | 8.4 | 11.6 | 1.7 / 2.0 | 2.1 / 2.2 | accuracy +0.5 (88% at 45 yd), indoors +0.3 |
| Cairo Santos | CHI vs NE (TNF) | 8.0 | 8.5 | 1.7 / 2.0 | 2.4 / 2.5 | NWS: 1 mph wind, 63F (shown, not a model input) |

Week-7 Sunday kickoffs were beyond the NWS range when this run was made. The cycle closer to kickoff fetches them.

## 6. Operating it

```
cd backend
uv run python ../scripts/sync_kicking_pbp.py --history --seasons 2019-2025   # once: backfill the history DB
uv run python ../scripts/sync_kicking_pbp.py --live                          # once: backfill the live season
uv run python ../scripts/kicker_projections.py reconcile
uv run python ../scripts/kicker_projections.py evaluate                      # writes data/models/kicker/kfp_evaluation.json
uv run python ../scripts/kicker_projections.py fit --register                # the season's artifact, approved
uv run python ../scripts/kicker_projections.py project --through-week 18
```

After the one-time backfill, the weekly learning cycle does `fit` (once per season) and `project` automatically, and
the refresh publishes the tables.

## 7. Limitations and follow-ups

* Under-prediction of about 0.38 pts/game from the league's rising long-range accuracy (biggest bias: retractable-roof
  games). Candidate fix: a season trend term in the conversion curve, validated before adoption.
* Weather is archived but unused. Re-test once a season of real NWS pregame snapshots exists, never with observations.
* No genuine pregame ESPN kicker benchmark for past seasons. The 2026 snapshots accumulate weekly.
* FG volume is the league rate for every team. Better opportunity signals (e.g. pregame totals with timestamps) are not
  freely available with verified timing.
* Fake-kick offensive points and D/ST-style events are not projected (they reconcile at 0 in 2026).
* Compressed range: projections sit roughly between 7 and 9.5 points because FG volume regresses to the league rate.
  That is the MAE-optimal point estimate on held-out data, but ESPN spreads kickers wider (week 7: Reichard 11.6 and
  Fairbairn 11.3 at ESPN vs 8.4 and 8.5 here). Ranking (Spearman 0.14) beats trailing averages, but separation at the
  top is modest.
* Not Start/Sit-ready: historical validation only.

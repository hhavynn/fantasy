# Projection movers

The `/movers` page answers one question: whose outlook changed between two stored forecasts, by how much, and what
was recorded in the data over the same period? It lists the biggest risers and fallers (QB/RB/WR/TE), for the
remaining season or the next week, with before -> after, positional rank movement and evidence, and links to each
player's page.

It adds no forecasting algorithm, changes no model, sync or schema and writes nothing. It is useful whether or not any
model beats ESPN: it describes how a projection moved, never whether it was right.

```
GET /api/projections/movers?source=espn|independent&view=ros|week&position=QB|RB|WR|TE&baseline=<id>&limit=15&season=
  app/analytics/projection_movers.py   movement(): which two snapshots, the window, per-player movement
  app/analytics/mover_evidence.py      attach(): signals recorded over the same period, for listed risers and fallers
  app/analytics/projection_attribution.py  attach(): why a Fantasy Intel forecast changed (risers, fallers, &player=)
page /movers                            app/movers/*, lib/projection-movers.ts
```

Contract `projection-movers/v1`. Both modules are SQL + stdlib, so the hosted API computes the page on request.

## Sources

* **ESPN (default).** One snapshot per `snapshot_date` of `espn_projection_history` (ESPN's weekly projection of every
  remaining week, stored once per day by `sync_espn.py`). `taken_at` is the newest `synced_at` of the day; the
  snapshot's cycle week is the first regular-season week whose first kickoff is after it. Snapshot ids are dates.
* **Ours (opt-in, `source=independent`).** One snapshot per forecast cutoff week `C` of the production model
  (`ifp-2.1.0`): the rest-of-season cycle that ran with cutoff `C` stores one immutable run per target week. Per target
  week the snapshot uses the newest run of that cutoff that was made before the week's first kickoff, is not rejected
  by the approved-artifact gate (`production_forecasts.artifact_gate`) and whose rows verify against their content
  hash (`integrity_problem`); a failed run is listed in `skipped` and never used. Only runs under the league's
  current scoring fingerprint are compared (others are listed in `excluded_runs`). A cutoff with only runs of the
  previous model (`ifp-2.0.0`, the fallback) is never a snapshot: movement across model versions is never shown. Snapshot
  ids are `w<C>`. Right after the switch to `ifp-2.1.0` (2026-10-18) only one `ifp-2.1.0` snapshot exists (cutoff week
  6), so Ours movers answer `available: false` with the reason ("Movement needs two stored ifp-2.1.0 forecasts ...",
  naming the `ifp-2.0.0` week-6 forecast it will not compare) until the week-7 cycle stores the second.

`current` is the newest snapshot. The default baseline is the newest snapshot from an earlier cycle week ("last week's
outlook"), else the newest older snapshot of the same week (with a note); `baseline=<id>` picks another stored one,
which must be strictly older (else 422). With fewer than two snapshots the page says so: a missing forecast is never
reconstructed.

## The comparison

* **Window.** The target weeks both snapshots forecast, from the current cycle week through the league's final
  scoring period (ESPN) or the last week the runs cover (ours), whose first kickoff is known and after the latest
  snapshot. So both totals cover the identical weeks, and neither forecast saw any of those games. Every other week is
  listed with its reason ("kicked off before the latest snapshot", "not in the earlier snapshot", ...). A week dropping
  off the schedule therefore never looks like a projection change.
* **Next week** (`view=week`) is the first window week only. For ours it compares the earlier cycle's forward
  estimate of that week (horizon 1, experimental) with the latest pregame forecast (horizon 0).
* **Values.** ESPN's number; ours is the expected points of a `projected` row and 0 for a bye or withheld-unavailable
  row. A missing ESPN row on the player's schedule bye counts 0 and is flagged per side (`bye_before`, `bye_after`;
  `bye` is a bye in both), since a trade can leave a real projection in only one. Anything else missing makes the player
  not comparable (counted by reason, never given a partial total).
* **Metrics.** Window totals before and after, change, percent change (only when the earlier total is at least 1
  point), per-week averages, rank within position by window total in each snapshot, among only the players comparable
  in both (a player missing from one side never shifts anyone's rank). A change under 0.05 is unchanged;
  the risers and fallers lists need a change of at least 0.5 points and at least 3 points per week on either side.
* **Ours components.** With `p` = P(appears) and `x` = points if he plays, a week's change splits exactly into
  availability `(p1 - p0) * x0` and per-game outlook `p1 * (x1 - x0)`. This is arithmetic on the model's own stored
  terms, not an attribution guess. ESPN publishes no components.
* **Scoring.** Ours compares matching scoring fingerprints. ESPN does not store the scoring with each snapshot, so the
  page says a scoring change between two ESPN snapshots could not be told apart from a projection change.

## Evidence

For each listed riser and faller, `mover_evidence` reports what was recorded over the same period: games with a
final score that kicked off between the two snapshots and at least three and a half hours before the latest one (so
the game was over when that forecast was made), against the player's last up to four games before the earlier one.

* **Usage** (`player_week_usage`): snap share, target share, carries per game, backfield rush share, route
  participation, xFP per game; shown only when both samples exist and the change passes a fixed threshold.
* **Availability**: injury-report designation (`nfl_injuries`), weekly roster status (`nfl_rosters_weekly`), and
  ESPN's injury status when it was refreshed between the two snapshots. ESPN keeps no history, so it is shown as the
  status at that check, never as a change and never as the headline; the same holds for an injury-report designation
  with no earlier report to compare with.
* **Ours components** are split so they add up to the shown change exactly; the rounding of stored expected points
  (up to 0.005 a week) is left in the per-game part.
* **Role**: team change, depth-chart rank at the position between the two snapshot times.
* **Schedule** (ours): a target week whose opponent differs between the two runs. A bye in both forecasts is
  informational only and never the headline. A bye in only one ESPN snapshot is named with its side (ours reports it
  as a coverage change).
* **Model** (ours): the two components above, and per-week coverage changes (e.g. withheld -> projected).

Nothing at or after the latest snapshot and nothing from a window week is read. Signals are labeled as observed over
the same period, not as causes; no football interpretation is generated. Only high-confidence NFL ID links are used.

## Why it changed (attribution, #225)

`projection_attribution.attach` adds `attribution` (contract `projection-attribution/v1`) to every listed riser and
faller and to the player named by `player=<espn id>` (the page passes the selected player). Everyone else keeps `null`.
SQL + stdlib, two queries for the rows and run metadata whatever the number of movers, nothing written.

**What the production model is and what each run stores.** `ifp-2.2.0` (and every earlier `ifp-*`) predicts points if
he plays as `x = max(intercept + sum_i coef_i * (value_i - mean_i) / std_i, 0)` per position (QB: a start/no-start blend
weighted by the chance he starts), and expected points `e = p * x` with the participation model's `p`. A stored run row
keeps `p`, `x`, `e` (2 decimals), its coverage status and reasons, its missing inputs and `explanation`: the four
largest input terms `coef_i * (value_i - mean_i) / std_i` (at least 0.05 points) with each input's value (null when it
was unknown and imputed). Run metadata keeps the model version, the artifact sha, the participation version, creation
time, data cutoff (`data_through_*`), cutoff week and horizon, and `evaluation.rescored_from` for a run re-scored to a
league's scoring. The full input vector, the intercept and the coefficients are **not** stored, the model is retrained
every week, and nothing here rebuilds a feature.

**The split of a compared week**, adding up to the mover's change to the cent:

| part | when | exactness |
|---|---|---|
| status | one forecast projected him, the other did not (bye, withheld) | measured; cites only the reason stored on the row |
| availability | `(p1 - p0) * x0` | exact (the movers' split) |
| input `f` | same model version and artifact, neither re-scored, neither floored at 0, `f` stored in both rows | `p1 * (c1 - c0)`, exact for RB/WR/TE (linear before the floor); QB labelled approximate |
| model changed | different model version, retrained artifact, unknown artifact, re-scored; or a different participation model/method, including a stored modifier on one side only (QB calibration map, availability news, a borrowed median) (availability) | the whole per-game (or availability) change, never split between inputs; the inputs both rows stored are listed with values only |
| residual | inputs stored in one row or neither, a floored row, a missing row or chance, a failed as-of check, and the rounding of stored 2-decimal values | each reason with its points |

A requested `player=` is kept in `players` even beyond the 300 largest changes; `requested_player` (present only when
`player` is given) says whether he is listed, filtered out by the position filter or not comparable (with the reason),
so a stale link never silently shows someone else.

`status` is `explained` (residual within `tolerance`: 0.005 per stored rounded number used), `partial`, `model_changed`,
`not_decomposed` (nothing measured) or `unavailable` (ESPN: one total per week and no inputs, labelled "ESPN", never as
Fantasy Intel's). Missing inputs are listed per forecast with their stored reasons; an imputed input shows no value and
says it was unknown, never 0.

**As-of guarantee.** Only the two stored runs are read, never a game outcome. A week is split only when both runs were
created strictly before the week's first kickoff (and before the player's own kickoff when the row stores it) and, where
a run records its data cutoff, that cutoff is before both the compared week and the run's own cutoff week. Otherwise the
week is residual ("as-of check failed") and listed in `as_of.refused`.

**Not done (needs stored inputs).** Because the model is retrained weekly, week-over-week comparisons normally show
"model changed". Splitting such a change between new inputs and new coefficients needs each run's full input vector and
coefficients stored with the run (a write-path change to the run tables, the league-file split and the re-score copy);
see issue #225.

## Safeguards

ESPN is the default. Ours carries the production governance (owner override; not shown to be better than ESPN) and an
experimental label beyond the next week. No confidence scores, grades, probabilities or roster recommendations.

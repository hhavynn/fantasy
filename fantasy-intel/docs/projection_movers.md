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
page /movers                            app/movers/*, lib/projection-movers.ts
```

Contract `projection-movers/v1`. Both modules are SQL + stdlib, so the hosted API computes the page on request.

## Sources

* **ESPN (default).** One snapshot per `snapshot_date` of `espn_projection_history` (ESPN's weekly projection of every
  remaining week, stored once per day by `sync_espn.py`). `taken_at` is the newest `synced_at` of the day; the
  snapshot's cycle week is the first regular-season week whose first kickoff is after it. Snapshot ids are dates.
* **Ours (opt-in, `source=independent`).** One snapshot per forecast cutoff week `C` of the production model
  (`ifp-2.0.0`): the rest-of-season cycle that ran with cutoff `C` stores one immutable run per target week. Per target
  week the snapshot uses the newest run of that cutoff that was made before the week's first kickoff, is not rejected
  by the approved-artifact gate (`production_forecasts.artifact_gate`) and whose rows verify against their content
  hash (`integrity_problem`); a failed run is listed in `skipped` and never used. Only runs under the league's
  current scoring fingerprint are compared (others are listed in `excluded_runs`). A cutoff with only runs of the
  previous model (`ifp-1.0.0`) is never a snapshot: movement across model versions is never shown. Snapshot ids are
  `w<C>`.

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
  row. A missing ESPN row on the player's schedule bye counts 0 and is flagged. Anything else missing makes the player
  not comparable (counted by reason, never given a partial total).
* **Metrics.** Window totals before and after, change, percent change (only when the earlier total is at least 1
  point), per-week averages, rank within position by window total in each snapshot. A change under 0.05 is unchanged;
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
* **Schedule** (ours): a target week whose opponent differs between the two runs. A bye inside the window is in both
  forecasts, so it is informational only and never the headline.
* **Model** (ours): the two components above, and per-week coverage changes (e.g. withheld -> projected).

Nothing at or after the latest snapshot and nothing from a window week is read. Signals are labeled as observed over
the same period, not as causes; no football interpretation is generated. Only high-confidence NFL ID links are used.

## Safeguards

ESPN is the default. Ours carries the production governance (owner override; not shown to be better than ESPN) and an
experimental label beyond the next week. No confidence scores, grades, probabilities or roster recommendations.

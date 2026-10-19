# Signal shadows: ifp-fr-p1, ifp-dct-p1 and ifp-awd-p1 (prospective, research only)

Two research studies ended with candidates that only prospective evidence can confirm:

* [first_read_depth_chart_signals.md](first_read_depth_chart_signals.md) recommends two shadow variants of ifp-2.
* [adaptive_workload.md](adaptive_workload.md) section 9 recommends a decline-only workload blend.

Until this change nothing forecast them. `scripts/signal_shadows.py` now pre-registers all three, freezes them in
one write-once pregame ledger per week and scores them against the existing `challenger.SHADOW_GATE`. This is
research shadow output only: production (`projection_source.py`, ifp-2.0.0) is unchanged, and nothing in the
API or the frontend reads it.

## Candidates (pre-registered before any prospective week)

| Candidate | Change to the frozen ifp-2 spec | Positions | Spec in code |
|---|---|---|---|
| `ifp-fr-p1` | adds `fr_team_share`, `fr_own_share` (FTN first reads, empirical semantics: `"1"` = first read) | RB, WR | `usage_signals.PROSPECTIVE` |
| `ifp-dct-p1` | adds `dc_tier` (static pregame depth tier, 1.5 h point-in-time snapshot) | QB, RB, WR | `usage_signals.PROSPECTIVE` |
| `ifp-awd-p1` | replaces `xfpl_eb` with `aw_role_d`, the decline-only adaptive role | RB, WR, TE | `adaptive_workload.AWD_*` |

What the candidates share:

* Every candidate keeps ifp-2's features, frozen penalty and recency half-life per position. Nothing is
  searched. Missing source data goes through ifp's automatic missing indicator, exactly as in the research.
* A position outside a candidate's list is not forecast by it. Its ledger column stays null, never a copy of
  ifp-2.
* The depth-change features (`dc_tier_change`, `dc_vacancy`, `dc_snap_gap`) were rejected and are in no
  candidate. TE is excluded from both signal candidates because it had no development gain.

**The decline-only role** (`RoleConfig(decline_only=True)`) uses the AW_B estimator: exposure on and the
team-change factor ½.

* Only a streak of two or more face-value games at least 25% *below* the established role is a regime. It is
  blended with weight n/(n+2), as before.
* A streak above the role is ignored, so `aw_role_d ≤ aw_eb` always (tested).
* It was pre-registered after the cohort asymmetry the study found. For that reason it is never scored on the
  historical windows: only prospective weeks count. It is kept out of `adaptive_workload.CANDIDATES`, so the
  rejected study and its cache are unchanged.

**Frozen spec.** On first use, each candidate's spec is written once to `frozen/<candidate>.json`. Every later
run must match its `config_hash`, or it is refused. The spec holds:

* the features and fit per position;
* the source constants (read semantics, FTN priors, depth cutoff and staleness, regime constants);
* the freeze rule;
* the ifp-2 config hash and the league-scoring fingerprint.

## When it runs (no new scheduler)

| Job (existing) | Runs | Does |
|---|---|---|
| `refresh_all.py` evidence stage (every refresh and pregame-guard run) | `signal_shadows.py pregame --week <upcoming>` | the gated, write-once freeze |
| `learning_cycle.py`, stage `signal_shadows` after `ctx_shadow` (Tue-Thu) | `signal_shadows.py cycle --week <target>` | scores fully final frozen weeks, records a missed week, downloads missing earlier-season raw files, retries the freeze (normally deferred) |

**Freeze rule.** The week is frozen at the first run where at least 50% of the teams that have not kicked off
do so within 6 h. In practice that is the Sunday-morning refresh. Freezing late gives three things:

* the freshest pregame depth snapshot;
* time for the previous week's FTN charting to land (nflverse pulls it 40-71 h after Monday night);
* ifp-2's features the week's final injury report.

Teams that already kicked off are excluded. As with ifp-ctx-p1, Thursday games (and international games
before that run) usually fall outside the ledger. Weeks before 2026 week 7 are never frozen.

**Just before the freeze**, the current season's FTN, play-by-play and depth-chart files are downloaded from
nflverse into content-addressed snapshots (`usage_signals.write_snapshot`). If a download fails, the last
stored snapshot, which is also pregame, is used and a warning recorded. A candidate whose historical snapshots
are incomplete abstains that week and gets a warning.

## Point-in-time capture

* **FTN.** Only games whose charting nflverse had fully pulled strictly before the forecast time count
  (`restrict_to_available`). A prior game that is not charted yet contributes nothing: it is never a zero and is
  never backfilled after the freeze. The ledger lists every such game. Note that nflverse rewrites
  `date_pulled` on a season re-pull, so in practice the guard is "the game is in the file we just downloaded".
* **Depth.** Each team uses the latest snapshot strictly before its kickoff minus 1.5 h, at most 8 days old,
  and never a snapshot timestamped after the freeze. Each row keeps its snapshot time and age. Because the
  freeze happens once, before the main slate, a Sunday-night or Monday team uses Sunday morning's snapshot,
  not one from closer to its own kickoff. This is the one place where prospective capture is staler than the
  research replay.
* **Training.** Training rows are the fully final weeks strictly before the target week. Their historical FTN
  and depth features are built exactly as in the research, including its optimistic assumption that the prior
  week was always charted.

## Files

All files live under `<IFP_MODEL_DIR>/research/signal_shadows/`, which is git-ignored.

| File | Mutability |
|---|---|
| `raw/<family>_<season>-<sha>.parquet`, `raw/manifest.json` | content-addressed; a changed upstream file becomes a new snapshot and the manifest keeps the history |
| `store.parquet` (+ `.meta.json`) | cache, rebuilt when any input fingerprint changes |
| `frozen/<candidate>.json` | write-once |
| `artifacts/s<S>w<WW>-<store fingerprint>.json` (the fitted models) | write-once; an interrupted run reuses it only when it was built from the same store, specs, cutoff and abstentions (otherwise it is refused, and new data refits into a new file) |
| `ledger/s<S>w<WW>-pregame.json` | write-once |
| `runs/*.json` (every attempt that checked something) and `missed/s<S>w<WW>.json` | write-once |
| `scores.json` | replaced on each scoring |

**Ledger provenance.** Each ledger records:

* every candidate's spec hash and the store fingerprint;
* a hash of the model code;
* the injury report, roster and panel hashes;
* every raw snapshot's sha and retrieval time;
* the FTN availability audit and the depth snapshot audit;
* the teams excluded because their game had already started;
* the FTN license notice.

Each row holds ifp-2's points, the three candidates' points and their inputs: `fr_*`, `ftn_games`, `dc_tier`,
the snapshot time and age, `aw_role_d`, `aw_streak_d` and `xfpl_eb`.

## Scoring and the gate

A week is scored once it is fully final, which means it appears in the store with labels. Scoring is on
identical rows: the player appeared and both ifp-2 and the candidate forecast him. Each in-scope position is
judged by `weekly_learning.shadow_criteria`, the criteria the ifp-2 shadow itself uses (`challenger.SHADOW_GATE`):

| Criterion | Requirement |
|---|---|
| Prospective weeks | ≥ 8 fully final weeks |
| Matched appearances | ≥ 400 per position |
| MAE | candidate − ifp-2, week-clustered 95% CI upper bound < 0 |
| Bias | \|bias\| not worse than ifp-2's by more than 0.15 |
| Starters pool | MAE not worse than ifp-2's |
| Interval coverage | 80% coverage between 75% and 85% |

Missing evidence is `insufficient_evidence`, never a pass. `eligible_for_review` is the most this can ever
yield: it is evidence for an owner review, not a promotion.

Two choices for the gate were fixed in advance:

* **Interval coverage.** The candidate's interval is ifp-2's frozen 80% band around the candidate's point.
  ifp-2 withholds its own QB intervals (74% confirmation coverage), so a `dct` QB pass also needs its
  prospective coverage to be inside 75-85%.
* **Starters pool.** The pool is ranked among the matched appearances, as for ifp-ctx-p1.

`scores.json` also reports two things that do not gate:

* **One cohort per candidate.** These are FTN-covered rows for `fr`, backup QBs on the chart (`dc_tier > 1`) for
  `dct`, and the decline regime (`aw_streak_d ≤ −2`) for `awd`.
* **A cross-check against the production ifp-2.0.0** actually frozen before kickoff (the weekly ledger's earliest
  pregame run).

**First check on real data (2026-10-18, scratch directory, not counted).**

* The fetch plus the full store build took 49 s, and a freeze took 46 s, well inside the 15-minute evidence
  deadline.
* Week 7 produced 434 rows for 28 open teams, with forecasts from `fr` 247, `dct` 308 and `awd` 351.
* The same-run ifp-2 refit matched the production week-6 forecast on 54 of 57 shared rows. The 3 that differ are
  explained by injury reports that landed after production froze on Thursday.

## Isolation

* `signal_shadows.py` opens both databases read-only.
* It never writes a table, the ifp-1/ifp-2 artifacts, the weekly ledger, the registry or anything the API or
  frontend reads. Nothing in `app/` imports it (tested).
* `learning_cycle.py` runs it in its own process with the 45-minute research deadline, after `ctx_shadow`. Any
  failure is `failed_nonfatal` plus a warning, and the cycle status and exit code are unchanged (tested).
  `--no-signal-shadows` skips it.
* In `refresh_all.py` it is one more evidence step with the 15-minute deadline. Its own warnings (exit 3) are
  `INFO` lines, and a failure is an ordinary evidence warning.

## Open items for the owner (not code decisions)

1. **FTN read-code semantics** ([nflverse/nflreadr#325](https://github.com/nflverse/nflreadr/issues/325)). The
   candidate uses the *empirical* reading (`"1"` = first read). Upstream has not confirmed either reading, and
   a silent relabelling would invert the feature. Prospective evidence for `ifp-fr-p1` is only meaningful under
   the reading it was frozen with.
2. **FTN licensing.** FTN charting is CC-BY-SA 4.0 ("FTN Data via nflverse"). The ledgers, store and raw
   snapshots hold FTN-derived numbers in a git-ignored local directory and are never published or synced to
   MotherDuck. ShareAlike obligations attach when adapted material is shared, so this internal use does not
   appear to be blocked, and the `fr` shadow is built. Publishing any FTN-derived number (a feature, a ledger,
   or a projection that uses them) needs the attribution and a share-alike review first. If the review
   concludes that even internal storage is not acceptable, stop collecting FTN data:
   * remove `ftn` and `pbp` from `FETCH_FROM`, and `ifp-fr-p1` abstains with a warning;
   * delete `raw/ftn_*` and the ledgers' `fr_*` columns.
3. **Depth-chart terms.** The modern depth charts appear to come from ESPN via nflverse. The upstream terms are
   unverified, so they are internal research input only.

## Manual use (from `backend/`)

```
uv run python ../scripts/signal_shadows.py prepare                                   # downloads + store warm-up
uv run python ../scripts/signal_shadows.py pregame --week W                          # what refresh_all runs
uv run python ../scripts/signal_shadows.py pregame --week W --now 2026-10-25T16:00Z  # the rule at a time (dry run)
uv run python ../scripts/signal_shadows.py cycle   --week W                          # what learning_cycle runs
uv run python ../scripts/signal_shadows.py score
uv run python ../scripts/signal_shadows.py status
```

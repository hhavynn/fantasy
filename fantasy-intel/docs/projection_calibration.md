# ESPN Projection Accuracy & Calibration Lab

This is a read-only measurement tool, not a projection model. It measures ESPN's
weekly league-scoring forecasts against stored actual fantasy points. It neither
changes data nor participates in player scores, signals, ROS totals, waivers,
lineup evaluation, or trades. It runs offline against an existing DuckDB file.

## Run

From `fantasy-intel/backend`:

```bash
uv run python ../scripts/backtest_espn_projections.py --season 2026
uv run python ../scripts/backtest_espn_projections.py --db /path/to/existing.duckdb --season 2026
uv run python ../scripts/backtest_espn_projections.py --db /path/to/existing.duckdb --raw-archive-root /path/to/data/raw/espn --season 2026
uv run python ../scripts/backtest_espn_projections.py --season 2026 --json
uv run python ../scripts/backtest_espn_projections.py --season 2026 --as-of 2026-10-14T12:00:00Z
```

`--db` defaults to the app's configured `FANTASY_DB_PATH` (including its ignored
local `.env`). The CLI never writes that file or configuration. If no database
exists, it prints an **insufficient historical observations** report without
creating a database. Missing tables/columns are reported, not migrated. A busy,
corrupt, or unreadable database produces an error and exit status 2. An
insufficient report exits 0: absence of evidence is a legitimate result.

The existing raw ESPN capture directory defaults to the database parent's
`raw/espn` directory. Use `--raw-archive-root` if a custom database lives elsewhere.
Archives must be present to verify completed actuals; a fresh row timestamp alone
is insufficient. The utility reads them and never creates, edits or repairs them.

`--as-of` must include a timezone. It freezes the eligibility cutoff for repeatable
analysis of the same stored data; it cannot reconstruct old actual-score or
schedule revisions. Omit it to use current UTC. Prefer one season and one scoring
system at a time. The API `backtest(connection, ...)` also accepts a read-only
DuckDB connection and returns only aggregates; the CLI's `--json` contains no
player IDs, names, league names, cookies, or player-level errors. Do not commit
private databases or generated reports even though the CLI output is aggregate.

## Audit and available evidence

The current schema provides:

| Source | Evidence | Important limit |
|---|---|---|
| `espn_projection_history` | `projected_points`, `synced_at`, season/player/scoring period | One row per date/player/week; latest same-day sync wins |
| `espn_player_weekly_points` | League-scoring `actual_points`, row `synced_at` | Actual and forecast share a mutable row timestamp; no final-score flag |
| `nfl_rosters_weekly` | Exact season/week team, position, ESPN/GSIS IDs | A missing or conflicting historical relationship must not be guessed |
| `player_id_map` | Stable ESPN-to-GSIS crosswalk | Only high-confidence ID-based mappings without conflict notes are fallback evidence |
| `nfl_schedules` | Game date/time, teams, game type, scoreboard | May be absent, incomplete, or have stale corrections |
| `leagues` | Season's current ESPN scoring period | Weekly points are not keyed by league; multiple leagues in one season are ambiguous |
| `sync_runs` + existing `data/raw/espn/<run>/` | Successful sync capture times, archived closed-week league response, actual-score payloads | Missing archives cannot be reconstructed from mutable database timestamps |

Audit the **actual file supplied**, not an example league or documentation claiming
a sync once worked. In the implementation environment there was no real league
database. The setup-only synthetic database had dated forecasts and prior-week
actuals with **zero overlapping player-weeks**, and no historical NFL rosters or
schedule table. Its rows are not empirical ESPN evidence. No real-data accuracy
or calibration conclusion was possible.

## Eligibility and lookahead prevention

The observation key is `(season, espn_player_id, scoring_period)`. Each candidate
receives one primary exclusion reason; aggregate audit counters separately
describe snapshots and scores. Candidate counts include actual-only and
history-only player-weeks, so missing forecasts and outcomes remain visible.

1. Resolve the player's **that-week** NFL roster via its direct ESPN ID. If absent,
   a unique high-confidence ID crosswalk may resolve its GSIS ID to the exact
   season/week roster, provided that roster has no conflicting explicit ESPN ID.
   Ignore postseason roster entries when `season_type` is
   present. Require a unique team/position/identity and QB, RB, WR, or TE. Never
   substitute `fantasy_players.pro_team`, a latest roster, or a name match.
2. Require exactly one regular-season NFL game for that team and week. This
   deliberately supports only ESPN weekly scoring periods corresponding to NFL
   regular-season week numbers. Byes, missing schedules, conflicting games,
   nonstandard period mappings, and unresolvable historical teams are excluded.
3. Establish kickoff from nflverse `gameday` plus `gametime`. The official
   [schedule dictionary](https://raw.githubusercontent.com/nflverse/nflreadr/master/data-raw/dictionary_schedules.csv)
   defines the clock as **Eastern time regardless of game location**. Convert
   with `America/New_York`, including DST. Reject missing, malformed, ambiguous,
   or nonexistent local clocks. Do not invent a midnight or week-wide kickoff.
4. Normalize repository naive `TIMESTAMP` capture times as UTC, matching
   `app.db.connection.utcnow`; timezone-qualified values retain their true UTC
   instant. Use **`synced_at`, never `snapshot_date`**, for forecast eligibility.
   A snapshot must be strictly before kickoff and no later than the cutoff.
   Exact-kickoff and later rows are counted as after-start predictions and never
   scored as pregame forecasts. ESPN's original publication time is not recorded:
   horizons describe when this app captured the forecast.
5. Require a finite, non-NULL actual; both finite NFL scoreboard scores; the
   eligibility cutoff at least **24 hours after kickoff**; a unique league
   context whose current scoring period is **later** than the evaluated period;
   and an actual row timestamp at least 24 hours after kickoff and no later than
   the cutoff. These are preliminary completion guards; the shared row timestamp
   does not prove that the actual value was refreshed. Already-ended games in the
   still-open ESPN week are omitted. The final season period can remain
   ineligible if ESPN never advances its current-period field.
6. Require the **same actual value** in an existing raw ESPN payload associated
   with an `ok` ESPN `sync_runs` entry. Its archived `league.json` must identify
   the same league/season and already show a later scoring period when fetched.
   Its capture must meet the 24-hour guard and finish no later than the cutoff.
   Read actual stats (`statSourceId=0`, `statSplitTypeId=1`) from archived player
   cards, weekly rosters or pool payloads. Never use archived projections as
   outcomes. If this evidence is absent, mismatched, corrupted, from an open
   week, or from a failed run, exclude as `unverified_actual_provenance`.
7. Choose the latest finite pregame forecast per player-week. If identical
   capture times contain conflicting forecast values, exclude the observation
   instead of choosing an arbitrary row. Duplicate identical forecasts cannot
   increase its weight. No fallback to the mutable current projection is allowed.

Projection writes can refresh the weekly row timestamp without changing an
in-game actual. That timestamp **alone never makes an observation eligible** or
increments the completed-actual count. The matching closed-week raw capture is
required as independent value provenance. The existing sync records its write
capture time after fetching; the archived league payload's already-closed period
establishes that the historical actual request followed period closure. Filesystem
modification times are not evidence. Run IDs are restricted to safe directory
names and archive symlinks escaping the selected root are refused.

Missing archives can make otherwise useful stored data ineligible. Preserve
existing raw captures rather than recreating them from present-day database rows.
Later official stat corrections can still change outcomes; this tool does not
claim to reproduce the exact final score knowable on any historical date.

`NULL`/absent projections or actuals mean unknown. Measured `0` and legitimate
negative fantasy scores remain observations. NaN/infinite values are excluded.

## Metrics, horizons and cohorts

For each selected observation, error = **actual minus projected**:

- **MAE**: mean absolute error, in league fantasy points.
- **RMSE**: square root of mean squared error, in league fantasy points.
- **Signed bias**: mean error; positive = ESPN under-projected, negative =
  ESPN over-projected. Positive and negative errors can cancel.
- **n**: unique eligible player-weeks, with distinct player-season, NFL game,
  and season-week counts alongside the overall result.

Report overall metrics and separate QB/RB/WR/TE metrics. Empty groups have `n=0`
and **NULL metrics**, never zero errors.

Forecast horizons are `[0,24)`, `[24,72)`, `[72,168)`, and `[168,infinity)` hours
before kickoff. Exactly zero hours is excluded by the pregame rule. Each bin
selects its latest eligible snapshot **once per player-week**. A player-week can
appear in several bins; summing bin counts would duplicate observations.
Raw horizon pools can differ in composition. Pairwise reports therefore compare
only player-weeks present in **both** bins and return each bin's metrics and
`far_minus_near_mae` on that shared set. A positive difference means the further
forecast had larger absolute errors in that observed set, not a causal finding.

For a transparent, pregame-defined **scoring-volume proxy**, report errors above
and below a projected-point cutoff, separately within each position. Default:
`--high-projection-threshold 10`; equality belongs to the higher group. This is
a configurable diagnostic partition, not a multiplier, learned parameter, or
universal starter definition. It does **not** establish actual NFL workload or
starter status; that question needs reliably archived pregame role/usage data.
Never define cohorts using the eventual actual score, which would select on the
outcome being evaluated. League scoring and positions affect point levels.

## Sample sufficiency and future calibration

Every group shows its sample count and a reporting status. The default
`--min-samples 30` is a conservative **display floor**, not a power calculation
or proof that 30 correlated player-weeks suffice. Below it, the status is
**insufficient historical observations**; above it, it is only **descriptive
sample available**. Nonempty thin groups still show their descriptive metrics,
with this warning. No confidence interval, p-value, or calibration factor is
invented. Players repeat across weeks and errors within one game are correlated.

Before considering any future numerical adjustment:

1. Collect genuine pregame snapshots across multiple completed weeks, retain
   the existing raw ESPN captures (enabled by default in `run_espn_sync`), and keep
   schedule and historical roster coverage current using the existing supported
   sync workflow. Once-per-day history may erase an earlier pregame capture if
   another sync runs after kickoff that day. This tool cannot recover it.
2. Inspect exclusions, distinct games/weeks, position/cohort counts, scoring
   settings, and paired horizon coverage. Check for selection effects: only
   players with reliable IDs, rosters, forecasts and outcomes enter the report.
3. Confirm actual-score freshness and corrections independently. Better future
   evidence would archive immutable capture times, historical team/role metadata,
   and actual-specific post-final provenance in a separate ingestion change.
4. Predefine a training period, retain later weeks as an untouched holdout, and
   assess uncertainty while accounting for repeated players/shared games. Check
   whether a direction of bias persists across weeks and seasons. A small average
   bias can coexist with large individual errors and does not justify a trade
   valuation adjustment.

Those are future evidence requirements, not work this utility performs. The
lineup/trade evaluator and production valuation behavior are unaffected.

## Tests

```bash
cd fantasy-intel/backend
uv run pytest tests/test_projection_backtest.py
uv run pytest
```

Tests use temporary synthetic DuckDB inputs to assert numeric errors, pregame
boundaries, historical traded-player mapping, Eastern/DST conversion, completion
guards, missing/zero semantics, unique snapshot weighting, paired horizons,
thin samples, missing schemas, CLI behavior and byte-for-byte read-only output.
Synthetic test results never become reported empirical calibration evidence.

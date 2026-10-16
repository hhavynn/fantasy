# Injury evidence: retrieval provenance and pregame freshness

Workstream C. Diagnostics and an audit log only: no model input, injury probability or participation
output changes. Code: `backend/app/analytics/injury_freshness.py`, CLI `scripts/injury_freshness.py`,
tests `backend/tests/test_injury_freshness.py`, table `injury_retrievals` (create-only block in
`schema.sql` right after `player_id_map`, no `SCHEMA_VERSION` bump).

`scripts/refresh_all.py` runs `report` for the upcoming week on every refresh and prints it into the refresh
log (see "Weekly evidence routine (automated)" in `independent_projection_engine.md`); `--skip-evidence`
turns it off. It never runs `refresh --execute`.

## 1. Audit: what exists, what is missing

| Piece | What it does | Gap |
|---|---|---|
| `app/nfl/sync.py` (`RAW_DATASETS["nfl_injuries"]`) | `nflreadpy.load_injuries([season])`; `write_all` calls `replace_frame_partition`, which DELETEs the whole season and re-inserts. | After a sync the table keeps only the latest download. Nothing records WHEN a team's rows were downloaded, so history of "what we knew when" is lost the moment the next sync runs. |
| `nfl_injuries.date_modified` | nflverse's per-row timestamp (PROVIDER time). `forecast_features.load_injuries` drops rows with `date_modified >= kickoff`. | It is provider time, not retrieval time: a report published Wednesday and downloaded Sunday after kickoff looks "pregame" by `date_modified`. Rows with a NULL `date_modified` are kept and cannot be dated at all. |
| `sync_runs` | One row per sync (start/finish/status/stats JSON). | Run-level, not per team, and the start time is not the download time of the injury dataset (the nflverse sync downloads ~15 datasets; ESPN's fetch takes minutes). |
| `fantasy_players.injury_status` (ESPN) | Overwritten on each ESPN sync; `synced_at`/`detail_synced_at` are set at WRITE time (`utcnow()` in `write_all`, after the fetch). | No history; the timestamp is later than the actual fetch; the status has no provider publication time at all. |
| `independent_projection_store.espn_designation_fallback` | For a team with no nflverse week-W `report_status` (after the pregame `date_modified` filter), uses ESPN OUT/DOUBTFUL/QUESTIONABLE (high-confidence id map) as the designation. | A team with no nflverse report AND no ESPN designation gets no rows, which the model treats the same as "nobody is listed". Absence of evidence is indistinguishable from "healthy". Early in the week nflverse rows often carry only practice status (no `report_status`), so the team is treated as unsynced. |
| `source_provenance.py` (PR #53) | Immutable pregame snapshots (`injuries_as_of`), `known_by` (= the snapshot's `captured_at`/retrieval semantics), `injury_timestamp_coverage` (share of rows with `date_modified`), `publication_time_status`, `source_meta` (`espn_status_fallback_teams`). | Snapshots are only taken when a learning cycle runs; between cycles there is no proof of when a given team was last fetched, and nothing flags a team whose evidence is old or absent before its kickoff. Superseded by section 5: new manifests carry per-team retrieval times. |
| `weekly_learning.py`, `scripts/learning_cycle.py` | Orchestrate sync, readiness, predict. `readiness()` checks box scores and finality, not injury-report freshness. | No injury-freshness gate or report. Exit 3 "not ready" never mentions injuries. |
| `pbp_exits.py` | In-game injury exits from play-by-play (completed games only). | Not a pregame source; unaffected. |

Real gaps this change closes: (1) no verified retrieval time per team, (2) publication time and retrieval
time are conflated or unknown, (3) no staleness measure against kickoff, (4) a team with no report is
not surfaced, (5) no safe way to plan a pregame retry.

## 2. The retrieval log (`injury_retrievals`)

One INSERT-only row per observation, written by the existing syncs right after their own commit
(outside the data transaction, so a logging failure becomes a sync warning and never rolls back data;
no existing table changes):

| column | meaning |
|---|---|
| `retrieval_id` | sha256 of source, scope, season, week, team, retrieved_at, content_sha, sync run. Primary key; write-once (`ON CONFLICT DO NOTHING`). |
| `source` | `nflverse` or `espn_status`. |
| `scope` | `team_week` (nflverse rows of one team and week), `team_current` (ESPN status of one team; ESPN status is a current snapshot so `week` is NULL), `feed` (the whole nflverse injury frame, so "we downloaded it and this team had no rows" is provable). |
| `fetch_started_at`, `retrieved_at` | OUR clock, naive UTC: before the download and when it returned (`fetch_all` captures them; they are never derived afterwards). For nflverse these bracket only the injury dataset; for ESPN they bracket the whole fetch (minutes), so ESPN age can be overstated by at most that window. |
| `content_sha`, `row_count` | sha256 and count of that scope's rows as downloaded. |
| `provider_published_at`, `rows_with_publication_time` | The provider's own timestamp: latest nflverse `date_modified` of the scope and how many rows carried one. NULL/0 when the provider supplies none. ESPN status never has one (the code refuses to store it). |
| `sync_run_id`, `recorded_at` | Join key to `sync_runs`; when the log row was written. |

Append-only semantics: identical content fetched again is a NEW row with the same `content_sha` and a
later `retrieved_at` (it proves the feed still said the same thing); rows are never updated or deleted by
any code (a test scans the module). Growth: one feed row plus one row per team-week (~600) per nflverse
sync and ~32 per ESPN sync. Failed downloads record nothing (the sync already warns about them).

ESPN completeness: the `team_current` rows are written only when the pro-player base (`pro_players`)
was fetched. If that fetch fails, the synced player set is just the FA pool and current rosters, so most
of a team's statuses were never refreshed; the sync then logs nothing for ESPN and adds a warning
(`injury status retrieval not logged ...`). An incomplete snapshot therefore never proves freshness: the
team falls back to its last complete ESPN retrieval, or to `missing`/`stale`.

Time zones: `nfl_injuries.date_modified` is `TIMESTAMP WITH TIME ZONE` after a real nflverse sync. The
module converts it to naive UTC in SQL (`timezone('UTC', date_modified)`, chosen from
`information_schema`), never in Python (pytz is not installed) and never with `::TIMESTAMP` (which
follows the session TimeZone), so the kickoff comparison is independent of the session TimeZone.

## 3. Freshness (`team_freshness`, `diagnostics`)

Per scheduled team (`nfl_schedules` REG, kickoff from `projection_backtest._kickoff`, Eastern to UTC,
None when ambiguous):

* Source that would be used, mirroring the ESPN fallback without importing or changing it: `nflverse`
  when the team has a week-W `report_status` designation not modified at/after its kickoff; else
  `espn_status` when an ESPN retrieval exists (counted mapped players exclude `player_id_map.notes`
  containing 'conflict', like `independent_projection_store`); else `none`.
* `status`: `fresh`; `stale` (evidence retrieved more than `stale_after_hours` = 24 before the reference
  time and kickoff within `window_hours` = 72 or unknown; the reference is now, or kickoff once the game
  started); `missing` (no report and no ESPN retrieval: NEVER healthy); `post_kickoff_only` (game started
  and the evidence was retrieved only at/after kickoff, even if the provider dated it before).
* A designation without any logged retrieval (data synced before this log existed) is `stale` with reason
  `nflverse_report_without_verified_retrieval`, never silently fresh.
* Output also carries both latest retrievals, hours since retrieval and to kickoff, the provider
  publication time (None when absent), counts of NULL publication times, `fallback_adds_no_designations`
  (ESPN fallback in use but no mapped OUT/DOUBTFUL/QUESTIONABLE players, i.e. the model sees an empty
  list) and `espn_fresher_than_unused_nflverse`.

Thursday vs Sunday: with the same 30-hour-old retrieval on Wednesday noon, the Thursday team (32h to
kickoff) is `stale` and the Sunday team (97h) is `fresh` with reason `kickoff_beyond_window`.

## 4. CLI

```
cd backend
uv run python ../scripts/injury_freshness.py report  --season 2026 --week 7 [--db PATH] [--json] [--now ISO]
uv run python ../scripts/injury_freshness.py refresh --season 2026 --week 7            # dry run (default)
uv run python ../scripts/injury_freshness.py refresh --season 2026 --week 7 --execute  # existing syncs only
```

`report` uses a read-only connection. `refresh` prints `plan_refresh`: teams needing a retrieval before
kickoff (stale or missing, not started), the sources (nflverse always; also `espn_status` when the team
has no nflverse designation), and teams already kicked off, which are never refreshable. `--execute`
only calls `run_nfl_sync` / `run_espn_sync` (no new feed, no paid source), refuses (exit 2) when nothing
is refreshable, and is refused (exit 2, with a message) when combined with `--now` (`--now` only
simulates a time; real syncs and their pregame labels use the actual clock). The two syncs run
independently: if one raises, the other still runs, the failure is reported per source under `errors`
(exit 1), and the affected teams are labeled `refresh_failed`, never `pregame`. Other teams are labeled by
comparing the refresh's completion time with their kickoff (`pregame`, `post_kickoff`, or `unknown_kickoff`). It does not recompute analytics; run
`compute_analytics.py` afterwards as `sync_nfl.py` does. A retrieval is never relabeled: `pregame` means
`retrieved_at < kickoff` strictly.

## 5. Integration: integrated

The log and diagnostics are now used by the pregame snapshot and the weekly learning orchestration.
Informational and provenance only: no model input, injury probability, participation output,
readiness status or exit code changes.

1. **Snapshot manifest** (`source_provenance.capture(..., con=)`; `weekly_learning.py predict` and so every
   learning cycle pass the live DB). New manifests carry a top-level `injury_retrievals` field built by
   `injury_freshness.manifest_view`: `{"schema": "injury-retrievals/v1", "log": "present" | "unavailable",
   "teams": {team: {source, retrieved_at, provider_published_at, content_sha, retrieval_id}}}`.
   The nflverse sync replaces the whole season on every run, so the captured table content is that of the
   latest successful nfl sync (`sync_runs`, source `nfl`, status `ok`), and only that sync's own retrievals
   can describe it. Per team the entry is the `team_week` retrieval whose `sync_run_id` equals that sync's
   run id, kept only if it is strictly before the capture time and strictly before the team's kickoff.
   No team gets an entry (nothing is guessed, and `known_by` falls back to the sync-level time) when there
   is no successful nfl sync, when that sync finished at or after `captured_at` (a backdated `--now`
   capture), when its log write failed (the sync only warns) or it did not list the team, when the kickoff
   is unknown, or when the retrieval is at or after kickoff. An older logged retrieval is therefore never
   attached to newer table content. `run_nfl_sync` commits the new content before it logs the retrievals and
   calls `finish('ok')`, so it now persists a `running` `sync_runs` row first (`SyncRun.start`, replaced by
   `finish`; every reader of `sync_runs` filters on `status = 'ok'`). If a sync started after the latest `ok`
   one is still `running` (the process died after the commit) or has any status other than `ok`/`error`
   (`error` is only set after a rollback), or a nflverse retrieval after it belongs to a sync with no
   recorded status (a crash from before this marker existed), no team gets an entry and `log` reads
   `unverifiable: ...`. `weekly_learning.py` then also reports the injuries part's sync-level `retrieved_at`
   as unknown, so `known_by` falls back to `captured_at`.
   The writer is the latest successful nfl sync **of the requested season** (read from the run's recorded
   `stats.season`; `run_nfl_sync` replaces only its own season, so a newer sync of another season is not the
   writer). `start()` records the season in the marker; an unfinished marker whose season is unknown is
   treated as possibly this season's (fail closed). `SyncRun.finish` replaces the marker with a single
   `UPDATE`, never DELETE then INSERT, so a failure while finishing cannot erase it.
   The same rule gates readiness (section 3): before kickoff a team's nflverse evidence counts as retrieved
   only through the writer sync's own retrievals. If the newest sync's log write failed, or the table is
   unverifiable (no successful sync of the season, an unfinished one after it), an older logged retrieval does
   not make the current content look fresh: the team is `stale`
   (`nflverse_report_without_verified_retrieval`) and the readiness report adds a warning ("nflverse injury
   table cannot be tied to a logged retrieval"). Informational only, as everything in this section. `log: "unavailable"` means an old database without the table. The field
   is part of the content-addressed manifest id, so a new retrieval gives a new manifest and, as for any
   changed input, a new ledger run; an unchanged log is a no-op rerun. The first capture after this is
   deployed also gets a new manifest id (its body gains `injury_retrievals`), which freezes one extra run for
   that week; this is harmless. Manifests are write-once and an existing manifest or part is never
   rewritten. Manifests made before this field existed have no such key; they load, reconstruct and
   rebuild identically (a capture without `con` also still produces the old shape). The schema string stays
   `source-snapshot/v1`: the field is additive and readers use `.get`.
   `known_by(manifest, part, team=None)`: for a team's `injuries/` part the verified `retrieved_at` of that
   team's entry replaces the sync-level upper bound from `sync_runs`, capped at `captured_at`, so it is never
   later than the capture. Without an entry the old rule (`retrieved_at`, else `captured_at`) applies.
   `injuries_as_of` evaluates it per team.
2. **Readiness warnings** (`weekly_learning.readiness(..., con=)`; `weekly_learning.py status/train/predict`
   and `learning_cycle.py` pass the live DB). `injury_freshness.readiness_notes` turns `diagnostics` into
   warning strings appended to `warnings`, plus a structured `injury_freshness` block (`log_available`,
   `upcoming_issues` = missing/stale teams whose game has not started, `post_kickoff_only`,
   `fallback_adds_no_designations`). The learning-cycle report carries them in `warnings`,
   `data.warnings` and `injury_freshness`, also when the cycle stops as not ready. They never add to
   `errors`, change `ok`/`pregame`, the cycle status or exit code, or any forecast row or inputs hash. If the
   `injury_retrievals` table is absent (old DB) or cannot be read, the single warning is "injury retrieval
   log unavailable ..." and the run continues.
3. **Fallback flag**: `fallback_adds_no_designations` (ESPN fallback in use but zero mapped
   OUT/DOUBTFUL/QUESTIONABLE players, so the model sees an empty list) is listed in the same warnings.
   `espn_designation_fallback` itself is untouched.

The cycle's `preflight` only checks configuration on the machine, before any data is read; the injury
warnings appear in the report at the readiness stage, where the target week is known.

## 6. Limits

* Retrieval time proves when WE fetched, not when the league published. Where nflverse provides
  `date_modified` we store it separately; where it is NULL we say so and do not impute.
* ESPN status is a point-in-time snapshot of the current status; its `retrieved_at` is the end of the whole
  ESPN fetch, and teams are ESPN's `pro_team` (the fallback itself keys teams by nflverse roster).
* nflverse team-week rows only exist for teams the feed lists; the `feed` row proves the download.
* Pregame labeling needs a known kickoff; ambiguous clocks yield `unknown_kickoff`, never `pregame`.

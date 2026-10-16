# Injury evidence: retrieval provenance and pregame freshness

Workstream C. Diagnostics and an audit log only: no model input, injury probability or participation
output changes. Code: `backend/app/analytics/injury_freshness.py`, CLI `scripts/injury_freshness.py`,
tests `backend/tests/test_injury_freshness.py`, table `injury_retrievals` (create-only block in
`schema.sql` right after `player_id_map`, no `SCHEMA_VERSION` bump).

## 1. Audit: what exists, what is missing

| Piece | What it does | Gap |
|---|---|---|
| `app/nfl/sync.py` (`RAW_DATASETS["nfl_injuries"]`) | `nflreadpy.load_injuries([season])`; `write_all` calls `replace_frame_partition`, which DELETEs the whole season and re-inserts. | After a sync the table keeps only the latest download. Nothing records WHEN a team's rows were downloaded, so history of "what we knew when" is lost the moment the next sync runs. |
| `nfl_injuries.date_modified` | nflverse's per-row timestamp (PROVIDER time). `forecast_features.load_injuries` drops rows with `date_modified >= kickoff`. | It is provider time, not retrieval time: a report published Wednesday and downloaded Sunday after kickoff looks "pregame" by `date_modified`. Rows with a NULL `date_modified` are kept and cannot be dated at all. |
| `sync_runs` | One row per sync (start/finish/status/stats JSON). | Run-level, not per team, and the start time is not the download time of the injury dataset (the nflverse sync downloads ~15 datasets; ESPN's fetch takes minutes). |
| `fantasy_players.injury_status` (ESPN) | Overwritten on each ESPN sync; `synced_at`/`detail_synced_at` are set at WRITE time (`utcnow()` in `write_all`, after the fetch). | No history; the timestamp is later than the actual fetch; the status has no provider publication time at all. |
| `independent_projection_store.espn_designation_fallback` | For a team with no nflverse week-W `report_status` (after the pregame `date_modified` filter), uses ESPN OUT/DOUBTFUL/QUESTIONABLE (high-confidence id map) as the designation. | A team with no nflverse report AND no ESPN designation gets no rows, which the model treats the same as "nobody is listed". Absence of evidence is indistinguishable from "healthy". Early in the week nflverse rows often carry only practice status (no `report_status`), so the team is treated as unsynced. |
| `source_provenance.py` (PR #53) | Immutable pregame snapshots (`injuries_as_of`), `known_by` (= the snapshot's `captured_at`/retrieval semantics), `injury_timestamp_coverage` (share of rows with `date_modified`), `publication_time_status`, `source_meta` (`espn_status_fallback_teams`). | Snapshots are only taken when a learning cycle runs; between cycles there is no proof of when a given team was last fetched, and nothing flags a team whose evidence is old or absent before its kickoff. The snapshot manifest carries no per-team retrieval time. |
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

## 3. Freshness (`team_freshness`, `diagnostics`)

Per scheduled team (`nfl_schedules` REG, kickoff from `projection_backtest._kickoff`, Eastern to UTC,
None when ambiguous):

* Source that would be used, mirroring the ESPN fallback without importing or changing it: `nflverse`
  when the team has a week-W `report_status` designation not modified at/after its kickoff; else
  `espn_status` when an ESPN retrieval exists; else `none`.
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
is refreshable, and labels each team by comparing the refresh's completion time with its kickoff
(`pregame`, `post_kickoff`, or `unknown_kickoff`). It does not recompute analytics; run
`compute_analytics.py` afterwards as `sync_nfl.py` does. A retrieval is never relabeled: `pregame` means
`retrieved_at < kickoff` strictly.

## 5. Not done / integration pending A or the architect

Forbidden files (`source_provenance.py`, `independent_projection_store.py`, `weekly_learning.py`, ...)
were not touched, so the snapshot manifest does not yet carry per-team retrieval times. Proposed interface
when those files are open:

1. `source_provenance.capture` adds `meta["injury_retrievals"] = injury_freshness.manifest_view(con, season, week)`:
   per team the latest `retrieved_at`/`content_sha` of each source strictly before that team's kickoff
   (a pure SELECT over `injury_retrievals`; add that small helper when integrating). Then `known_by` for
   injuries could use the verified `retrieved_at` instead of the capture time.
2. `weekly_learning.readiness` (or `learning_cycle` preflight) calls `injury_freshness.diagnostics` and
   reports `upcoming_issues` as a warning (not an error) so a Thursday team with stale evidence shows up
   in the cycle report.
3. `espn_designation_fallback` could flag teams where the fallback adds no designations.

## 6. Limits

* Retrieval time proves when WE fetched, not when the league published. Where nflverse provides
  `date_modified` we store it separately; where it is NULL we say so and do not impute.
* ESPN status is a point-in-time snapshot of the current status; its `retrieved_at` is the end of the whole
  ESPN fetch, and teams are ESPN's `pro_team` (the fallback itself keys teams by nflverse roster).
* nflverse team-week rows only exist for teams the feed lists; the `feed` row proves the download.
* Pregame labeling needs a known kickoff; ambiguous clocks yield `unknown_kickoff`, never `pregame`.

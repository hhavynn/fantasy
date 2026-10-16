# Evidence health

`scripts/evidence_health.py` answers one question without anyone reading logs: **is this Mac really
collecting the prospective evidence, week by week, and what must be fixed before the next kickoff?**
The logic is in `scripts/evidence_audit.py`. It is local operations tooling: the hosted API never imports
it, so it lives beside `learning_cycle.py` rather than in `app/`.

A feature merged on GitHub is not evidence collected on the Mac. `refresh_all.py` exits 0 even when its
evidence stage only logged `WARNING: Evidence: ...` lines. That is intended, so a broken benchmark never blocks
the data refresh. So a zero exit code proves the ESPN/NFL sync worked, not that the Start/Sit decision set or
the ESPN benchmark was captured. This tool checks every evidence substep separately. It does not change
refresh_all's failure rules.

```bash
cd backend
uv run python ../scripts/evidence_health.py                  # compact summary; exit 0 ok, 2 warnings, 1 failures
uv run python ../scripts/evidence_health.py status --json    # the full report (schema evidence-health/v1)
uv run python ../scripts/evidence_health.py status --hosted  # also read MotherDuck's schema_version/published_at
uv run python ../scripts/evidence_health.py status --notify  # one macOS notification per NEW warn/fail issue
uv run python ../scripts/evidence_health.py guard            # what the pregame guard would do now
uv run python ../scripts/evidence_health.py launchd verify   # launchd jobs installed as templated and loaded?
uv run python ../scripts/evidence_health.py launchd install  # idempotent install plan (prints; --apply runs it)
```

## What it reads (read-only)

| Question | Source |
| --- | --- |
| Is the scheduler installed, identical to the template, loaded? When did it last run? | `~/Library/LaunchAgents/com.fantasy-intel.*.plist` (parsed with `plistlib`, compared key by key, so `plutil` reformatting is not drift), `launchctl print gui/<uid>/<label>` |
| Did each scheduled firing run? | the calendar in the installed plist, matched to `data/logs/refresh-<UTC>.log` start stamps (a run up to 6 h late counts: a sleeping Mac runs the job on wake) and to the learning cycle's `reports/cycle-<UTC>-<pid>.json` |
| Did each evidence substep succeed? | the refresh log, parsed by refresh_all's own markers only (`==> step`, `<== exit N`, `WARNING: Evidence:`). A child's output between markers is never interpreted. A test runs the real `refresh_all.main()` and parses its log, which pins the format |
| Did the expected output files appear and verify? | `data/models/start_sit/decision_sets/` (both hashes checked; `load_decision_sets` silently skips damaged files, so they only show up here), `data/models/espn_benchmark/` (file name = sha256 of the bytes), `data/models/weekly/ledger/` and `missed/` |
| ESPN pregame snapshots | `espn_projection_history`, per NFL team, latest stamp before each kickoff. Only a stamp that falls inside a successful ESPN `sync_runs` row counts as a capture (for the week audit, the next-kickoff check and the guard); any other stamp is reported as unverified, never as present |
| Production forecasts | `independent_projection_runs` / `_rows` through `production_forecasts.pregame_forecasts`, the same reader every evaluation uses (model, scoring, artifact fitted before the week's first kickoff, content hash, created before each player's own kickoff) |
| Is ifp-2 served, or the ifp-1 fallback? | `projection_source.weekly(con, season, week, "independent")`, the function the API calls |
| Finality | `weekly_learning.week_finality`, the rule the benchmark counts weeks by (only for weeks whose games have all kicked off) |
| Schema / publication | `schema_meta.schema_version`; last publish from the refresh logs' `Published ... at` lines and `data/logs/publish-history.jsonl` (`publish_motherduck.py` appends a line per successful publish, manual ones included). `--hosted` reads the hosted `schema_meta`. It needs `MOTHERDUCK_TOKEN` and runs only when you ask |
| Disk | free space on the data volume, and a write probe in `data/logs` |

The live DuckDB is opened read-only. When a refresh holds the write lock, a temporary copy is read and the
report says so. Nothing syncs, trains, publishes or backfills. The only files written are under
`data/logs/evidence_health/`: the guard's attempt record and the alert de-duplication state.

## The per-week audit

From the first prospective week (week 6 of 2026, when collection started; `--from-week` overrides) through
the week of the next kickoff, each week reports:

* **kickoff slots**, the authoritative kickoffs from `nfl_schedules` (US Eastern clock, never guessed);
* **expected capture windows**: every scheduled refresh firing between the previous week's last kickoff and
  this week's last kickoff, and whether a refresh log started for it (`ran` / `missed` / `pending`);
* per slot: the latest **ESPN snapshot** before kickoff and its age, **valid ifp-2 pregame rows** (and the
  ifp-1 fallback's), whether a **frozen research forecast** covers it, whether the slot has league-rostered
  players and a **Start/Sit decision set** recorded before kickoff;
* **usable forecasts by position** (`coverage_status = projected`), which model the switch **serves**,
  **finality**, and **matched benchmark observations by position** (scored rows of the newest scorecard);
* **missing evidence with its cause**. Examples: "no refresh ran between X and kickoff; the scheduled run at Y
  left no log (Mac asleep or off, or the job was not loaded)", "refresh at X completed but its evidence stage
  did not run", "ifp-2 run created after this kickoff; the ifp-1 fallback has N pregame rows". A pregame
  snapshot that a refresh log reported but that is no longer stored is flagged too: a later same-day sync
  replaced it after kickoff. `espn_projection_history` keeps one row per player per local day. The pregame
  lock in `espn/sync.py` prevents this since commit 0ca8e30.

Missed evidence is **reported, never regenerated**. Misses in the latest two weeks are warnings; older ones are
`info`, because they cannot be recovered and repeating them would bury new problems.

## Issues and remediation

Every issue has a code, a severity (`fail` = act now, `warn` = gap or risk, `info` = context), a fingerprint
(`code:subject`) and, where one exists, a fix command.

| Situation | Code |
| --- | --- |
| job never installed / installed but not loaded / plist invalid / differs from template | `scheduler_not_installed`, `scheduler_not_loaded`, `scheduler_plist_invalid`, `scheduler_drift` |
| job installed but not executing | `scheduler_missed_runs`, `scheduler_last_exit`, `scheduler_program_missing` |
| refresh succeeded but evidence failed or never ran | `evidence_step_failed` (fail when the Start/Sit step failed, warn for the benchmark or injury report), `evidence_stage_not_run` |
| refresh failed, killed, unreadable | `refresh_failed`, `refresh_unfinished`, `refresh_unreadable`, `refresh_never_ran` |
| production run not written before kickoff | `production_forecast_missed`, `ifp_fallback_served` |
| missing ESPN pregame snapshot / stale / lost | `espn_snapshot_missed`, `espn_snapshot_stale`, `espn_pregame_snapshot_lost` |
| missing Start/Sit decision set or research forecast | `start_sit_decision_set_missed`, `research_forecast_missed` |
| week pending finality / final but no benchmark | `week_pending_finality`, `benchmark_not_recorded` |
| outdated hosted DB / schema mismatch | `hosted_outdated` (with `--hosted`), `hosted_maybe_outdated`, `hosted_schema_mismatch`, `local_schema_old` |
| corrupted evidence file | `evidence_file_corrupt` (fix: move it to `data/quarantine/`; never edit evidence) |
| snapshot timestamp provenance unverifiable | `snapshot_provenance_unverified` |
| low disk / write failure | `disk_low`, `filesystem_write_failed` |
| next kickoff not ready / a kickoff no scheduled run covers | `next_kickoff_unready` (warn within 48 h, fail within 6 h), `schedule_gap` |

**Notifications** (`--notify`) use the built-in `osascript` (Notification Center). No dependency, no network.
An issue is notified once while it stays open. `alerts.json` keeps only the open fingerprints, so an issue that
resolves and later returns is notified again.

## Missed capture windows and the pregame guard

The refresh template fires at **Thu 15:00, Sun 08:00, Mon 15:00, Tue 08:00** Mac local time, assumed Pacific.
Those fixed times miss kickoffs that they do not closely precede:

* **London/Germany games**, Sunday 9:30 ET (6:30 PT): the Sunday run comes 1.5 h *after* kickoff. Their latest
  capture is Thursday's, 63.5 h earlier;
* **Thanksgiving** 12:30 and 16:30 ET (9:30 and 13:30 PT): before the Thursday 15:00 run. The 20:20 ET game is
  covered;
* **Friday, Saturday and Christmas** games (late season): covered only by a run days earlier.

`status` lists these under `schedule_gaps` for the next 21 days. The fix is the **pregame guard**
(`evidence_health.py guard`, optional launchd job `com.fantasy-intel.pregame-guard`, every 30 minutes):

1. it reads the next kickoff from `nfl_schedules` (cheap, read-only; no network);
2. it does nothing until the kickoff is within **6 h** (`--lead-hours`), and stops trying **10 min** before it;
3. the slot counts as captured when a refresh that started within **12 h** of kickoff (`--max-age-hours`)
   completed with its Start/Sit step recorded (or nothing eligible), **and** an ESPN snapshot of the slot's
   teams landed in that span. A failing ESPN benchmark of a finished week is not a reason to refresh again;
4. it is a fallback, not a second schedule: when the loaded refresh job has a firing within those 12 h before
   kickoff that is still ahead (or under 45 min ago), it waits for that run. Thursday-night, Sunday and Monday
   kickoffs therefore cost no extra refresh. It acts only for kickoffs no scheduled run covers, or when the
   scheduled run left no capture;
5. otherwise it runs `refresh_all.py` once and records the attempt in
   `data/logs/evidence_health/guard-state.json`. It makes at most **3** attempts per kickoff (`--max-attempts`),
   **25 min** apart (`--retry-minutes`). It skips while a refresh is running, holds a lock against a second
   guard, and never touches a kickoff that has passed;
6. with `--notify` it posts one notification per new warn/fail issue, including "guard gave up before
   kickoff X".

There is no loop inside the process: each launchd firing does one check and exits. A sleeping Mac runs the
check on wake. A Mac that is off captures nothing. `pmset repeat wakeorpoweron` can wake it, but there is one
schedule per Mac, so the guard cannot cover a 6:30 PT London kickoff while the Mac sleeps until 6:55. Wake it
earlier on those Sundays, or run `refresh_all.py` by hand the night before.

## Safe local setup (launchd)

`evidence_health.py launchd install` prints an **idempotent plan** per job and changes nothing without
`--apply`:

| State found | Plan |
| --- | --- |
| not installed | `mkdir -p data/logs ~/Library/LaunchAgents`, write the rendered template, `plutil -lint`, `launchctl bootstrap` |
| installed exactly as templated, loaded | nothing (no duplicate job, no reload) |
| installed as templated, not loaded | `launchctl bootstrap` |
| installed but different (hand edits) | back the plist up to `<plist>.bak-<UTC>`, `launchctl bootout`, write, lint, bootstrap |
| installed for another checkout (its `WorkingDirectory` is not this checkout's `backend/`, e.g. run from a git worktree) | nothing; `--repoint` is needed to move the job here |

`--jobs` selects jobs (default `refresh,weekly-learning`; add `pregame-guard` to opt in). The plan prints the
drift, so a deliberate hand edit (for example an extra Wednesday firing) is visible before anything replaces
it. `launchd verify` checks installation, plist validity, template drift, the loaded state and the program
path, and exits 1 on any problem. Tests cover all of this against a temporary `LaunchAgents` folder and a fake
`launchctl`. Nothing in the test suite or the tool installs a service, changes a secret or publishes data.

# ifp-ctx-p1 prospective shadow: how the ledger is produced and read

The frozen research candidate `ifp-ctx-p1` (QB context for QB rows, QB + teammate context for RB/WR; pre-registered in
[qb_teammate_context.md](qb_teammate_context.md) section 8) can only be confirmed by write-once pregame forecasts from 2026
week 7 on. Until this change nothing ran `context_research.py predict` on a schedule. `scripts/ctx_shadow.py` now does,
as pure research shadow output: nothing in production, the API or the frontend reads it.

No new scheduler. Two existing jobs call `ctx_shadow.py`, which decides *when* to call `context_research.py predict` /
`prospective` and records why. It does not change the candidate's model, features or ledger format.

| Job (existing) | When | Runs | Does |
|---|---|---|---|
| `refresh_all.py` read-only evidence stage (launchd `com.fantasy-intel.refresh`, and every `pregame-guard` run) | Thu 15:00, Sun 08:00, Mon 15:00, Tue 08:00 PT, plus within 6 h of any kickoff the fixed times miss | `ctx_shadow.py pregame --week <upcoming>` | the gated, write-once pregame freeze; also after a failed sync |
| `learning_cycle.py`, side stage `ctx_shadow` after `kicker` (launchd `com.fantasy-intel.weekly-learning`) | Tue/Wed/Thu 07:00 PT | `ctx_shadow.py cycle --week <target>` | scores every fully final frozen week, records a missed week, retries the freeze (normally deferred mid-week); also when the cycle stops at `waiting_for_data`, `no_upcoming_week` or a failed production stage |

**Surviving a failed run.** A missed freeze is never backfilled, so the freeze does not depend on the rest of the
pipeline succeeding:

* *DuckDB lock.* On 2026-10-18 at 08:00 `sync_espn.py` could not open the database, because the social-trends launchd
  job (`uv run trends run` every 20 minutes, read-only) held it. A read-only reader still blocks a writer, and
  `connect()` only retried for 5 s. Now every `refresh_all.py` and `learning_cycle.py` step process waits up to
  `FANTASY_DB_LOCK_WAIT_S` for the lock, with backoff (`app/db/connection.py`; 120 s in refresh steps, 300 s in cycle
  stages). A refresh step that still fails on the lock is run again after 1 and then 3 minutes.
* *Failed sync.* `refresh_all.py` keeps running the other, independent sync. It skips analytics, the report, the
  publish and the full evidence stage, but it still runs every research shadow's pregame freeze (an evidence step
  labelled `<name> pregame week W`) on the stored data. The shadow's own freeze gate decides whether that injury evidence is good enough.
* *Data not ready.* `learning_cycle.py`'s separate models (the `kicker()` and `ctx_shadow()` stages) do not wait for
  the ifp readiness gate.
  When the cycle stops at `waiting_for_data`, both still run. The read-only ctx_shadow also runs after
  `no_upcoming_week` and after a failed production stage. The cycle's status and exit code never change.

A new research shadow plugs in the same way. In `refresh_all.py`, add an evidence step labelled
`<name> pregame week W`, or name the step `<name>_shadow ...`. `is_shadow` then also runs it after a failed sync. In `learning_cycle.py`, add a closure like
`ctx_shadow(after=None)` and call it next to `ctx_shadow()` in both places: after the production stages, and in the
early-stop block of the `CycleStop` handler.

**Freeze rule** (fixed in `ctx_shadow.py` before any prospective week exists). The ledger is write-once, and
the QB and teammate features read the week-W designations (Out / Doubtful / Questionable). A mid-week practice
report has none, so a forecast frozen on it would stay degraded forever and would not match the final reports
the replay windows were evaluated on. For the teams that have not kicked off, a team is *ready* when
`injury_freshness` reports nflverse designations retrieved `fresh`. The ledger is frozen when

* at least 90% of the open teams are ready, or
* the main slate is imminent (at least half of the open teams kick off within 6 h). It is then frozen anyway,
  and every team that is not ready is recorded as a warning.

Otherwise the freeze is deferred to a later run. It never runs without the week's report (`predict` refuses
too). Teams that already kicked off are excluded by `predict` itself. In practice the freeze happens at the
Sunday-morning refresh (or the pregame-guard run before an early international game), after the Friday and
Saturday final reports. **Thursday games, and international games that kick off before that run, therefore
usually fall outside the ledger.** One write-once file per week cannot hold them without freezing the Sunday
teams on a practice report. Weeks before 2026 week 7 (the pre-registered start) are never frozen.

**Early international games are covered once the pregame guard is installed.** The guard
(`scripts/launchd/com.fantasy-intel.pregame-guard.plist`; install it with
`evidence_health.py launchd install --apply --jobs pregame-guard`) runs a refresh within 6 h of a kickoff that no fixed
run precedes, e.g. overnight before a 09:30 ET Sunday game. That refresh syncs nflverse first. By then Friday's final
reports are in, so the freeze rule usually freezes the whole Sunday slate, the international teams included. Without
the guard, the Sunday 08:00 run is after those kickoffs and drops those teams.

**Per-slot ledgers (Thursday, and Friday/Saturday late in the season): not implemented, open follow-up.** It was
judged not cheap for 2026-10-18, so it is not done here. A Thursday team gets its final designations days before the
Sunday teams, so a correct freeze for it needs its own write-once file. That changes the pre-registered ledger
contract in more than one place:

* `context_research.py predict`: a team subset and a slot-named path such as `s<S>w<WW>-<slot>-pregame.json`. The main
  ledger must also exclude every team already frozen in a slot ledger, not just the teams that kicked off: a guard
  run can freeze the main slate before an international kickoff.
* `prospective`: it globs `s*-pregame.json`, so the rows of one week must stay disjoint across files.
* `ctx_shadow.py`: `ledger_path` / `already_frozen`, `check_missed` (a week is missed only if no file covers a team)
  and `score_alongside` (one entry per week, not per file).
* The freeze rule needs to be evaluated per slot.

Until that lands, the Thursday game's teams are excluded every week. That is about 2 of 28 teams; it slows the
evidence, it does not invalidate it. Week 7's Thursday game (2026-10-22) is the first one affected.

**Warnings.** When injury data is missing or stale, `ctx_shadow.py` does not forecast silently:

* it prints `WARNING:` lines and exits 3, which `refresh_all.py` logs as an evidence-stage warning;
* `learning_cycle.py` copies the warnings into the cycle report (`warnings`, prefixed `ifp-ctx-p1 shadow:`;
  full detail under `ctx_shadow`).

Cases that warn:

* frozen with any open team not ready (the teams and reasons are listed);
* no report while the main slate is close ("this week's ledger will be missed");
* no `injury_retrievals` log;
* unknown kickoffs;
* a week whose games all started without a ledger.

A deferral far from kickoff is a note, not a warning.

**Files** (all under `<IFP_MODEL_DIR>/research/context/`, git-ignored):

| File | Written by | Mutability |
|---|---|---|
| `ledger/s<S>w<WW>-pregame.json`, `artifacts/`, `frozen_ifp-ctx-p1.json` | `context_research.py predict` (unchanged) | write-once |
| `shadow/freeze/s<S>w<WW>.json` | the freeze: ledger sha256, the decision, the warnings and the full `injury_freshness` summary at freeze time | write-once |
| `shadow/runs/s<S>w<WW>-<UTC>-<kind>-<pid>.json` | every attempt that checked something (freeze, deferral, failure, scoring) | write-once |
| `shadow/missed/s<S>w<WW>.json` | a prospective week that started without a ledger (never backfilled) | write-once |
| `shadow/scores.json` | scoring, recomputed each cycle (stat corrections) | replaced |
| `prospective.json` | `context_research.py prospective` (unchanged): the pre-registered gate | replaced |

**Scoring.** Once a week is fully final (it is in the context store with labels), `cycle` runs
`context_research.py prospective`: the pre-registered gate of `qb_teammate_context.md` section 8, comparing ctx with the
ifp-2 refit frozen in the same ledger. `scores.json` adds a comparison on identical rows (appearances where all three exist), per week,
per position and pooled. Each entry has MAE and bias for:

* `ctx`: ifp-ctx-p1;
* `ifp2_same_run`: the ifp-2 refit frozen in the same ledger;
* `ifp2_production`: the production ifp-2.0.0 forecast actually frozen before kickoff, i.e. the `challenger`
  block of the earliest `pregame` run in the weekly ledger `data/models/weekly/ledger/s<S>w<WW>/`.

The gate decides; `scores.json` is a cross-check that the same-run refit tracks the forecast production really
froze.

**Isolation.**

* `ctx_shadow.py` opens both databases read-only.
* It never writes a table, the ifp-1/ifp-2 artifacts, the weekly ledger, the registry, or anything the API or
  frontend reads.
* `learning_cycle.py` runs it in its own process with a 45-minute deadline, after the production `project` and
  `kicker` stages. Any failure is `failed_nonfatal` plus a warning, and the cycle status and exit code are
  unchanged (tested). `--no-ctx-shadow` skips it.
* In `refresh_all.py` it is one more evidence step, with that stage's 15-minute per-step deadline.
* Re-runs are idempotent: once the week's ledger exists, nothing is called, written or recorded.

**Sibling shadows.** Two more research shadows use the same two jobs, the same non-fatal handling and the
same exit codes. They run right after this one.
* `qb_scenario_shadow.py`, ifp-ctx-p2q. It is the QB-only S2 candidate and uses this freeze rule unchanged
  ([qb_scenario_mixture.md](qb_scenario_mixture.md) section 8).
* `participation_cal_shadow.py`, the QB isotonic participation map
  ([participation_calibration.md](participation_calibration.md), "Prospective re-test").

`learning_cycle.py` runs them as the stages `qb_scenario_shadow` and `ipm_cal_shadow`. `refresh_all.py` runs
`qb_scenario_shadow pregame week W` and `ipm_cal_shadow cycle`.

**Manual use** (from `backend/`):

```
uv run python ../scripts/ctx_shadow.py pregame --week W                          # the gated freeze (what refresh_all runs)
uv run python ../scripts/ctx_shadow.py pregame --week W --now 2026-10-25T16:00Z  # evaluate the rule at a time (dry run)
uv run python ../scripts/ctx_shadow.py cycle   --week W                          # what learning_cycle runs
uv run python ../scripts/ctx_shadow.py score                                     # score fully final frozen weeks
uv run python ../scripts/ctx_shadow.py status                                    # ledgers, freezes, misses, latest run, scores
```

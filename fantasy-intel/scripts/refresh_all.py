"""Full data refresh: ESPN -> nflverse -> analytics -> projection-performance report -> MotherDuck (only
when MOTHERDUCK_TOKEN is set), then a check that this week's ESPN projections were snapshotted before the
first kickoff, then the read-only "evidence" stage (injury freshness report, start/sit decision set, ESPN
benchmark). launchd runs it on a weekly schedule (see README "Scheduled refresh"); it is also fine by hand:

    cd backend && uv run python ../scripts/refresh_all.py [--no-publish] [--check-only] [--skip-evidence] [--skip-report] [--skip-replay]

Report step (docs/projection_performance.md): after the analytics step, scripts/projection_performance.py
publish builds the accuracy scorecard from a read-only connection and inserts it as one write-once row, so the
MotherDuck publish that follows carries it to the hosted site. It can never fail the refresh: a problem is
logged as a WARNING and the publish still runs. scripts/model_replay.py publish follows it the same way (today's
model replayed on every fully final week, one snapshot row when the model or the weeks changed).

Each step runs in its own process. The two syncs are separate sources, so a failed ESPN sync still runs the nflverse
sync (and the other way round); a failure skips every step that needs the failed one (analytics, report, publish,
pregame check) and the run exits 1. Every run writes data/logs/refresh-<UTC stamp>.log with each step's output.

DuckDB lock (one writer, and a writer cannot open while any other process holds the file, even read-only, e.g. the
social-trends job): every step is launched with FANTASY_DB_LOCK_WAIT_S (default LOCK_WAIT_S), so app.db.connection
waits that long for the lock with backoff; a step that still fails on the lock is run again after
LOCK_RETRY_DELAYS_S (the syncs are idempotent upserts).

Evidence stage (docs/independent_projection_engine.md, "Weekly evidence routine (automated)"): it runs
only after every sync and analytics step has exited, so no write connection is open; it opens the
database read-only (here and in the four scripts it launches) and runs no sync. It can never fail the
refresh: each step's problem is logged as a WARNING and the exit code is unchanged. One of them is the
research shadow of the frozen ifp-ctx-p1 candidate (scripts/ctx_shadow.py pregame): it freezes that week's
write-once pregame ledger once the final injury reports are in, or the main slate is within 6 hours. The QB-only
scenario shadow (scripts/qb_scenario_shadow.py pregame, ifp-ctx-p2q) follows it under the same freeze rule, and the
QB participation-calibration re-test (scripts/participation_cal_shadow.py cycle) scores fully final weeks.
Another is the D/ST research shadow of dfp-r1 (scripts/dst_shadow.py pregame): it freezes that week's write-once
D/ST ledger once the lines and the previous week's ESPN statistics are in, if the learning cycle has not already.
The signal shadows (scripts/signal_shadows.py pregame: ifp-fr-p1 / ifp-dct-p1 / ifp-awd-p1) freeze their write-once
ledger once the main slate is within 6 hours, after re-fetching the season's FTN, play-by-play and depth-chart files.
The injury-return shadow (scripts/injury_return_shadow.py pregame: ifp-irs-p1 / ifp-irb-p1) freezes its write-once
ledger under the same freeze rule.
The frozen pregame point-distribution ledger (scripts/distribution_ledger.py freeze, research only) is a retry of
the freeze learning_cycle.py normally makes right after its pregame forecast.
The research shadows' evidence steps (labelled "<name> pregame week W" or named "<name>_shadow ...") also run after
a failed sync, on the data already stored: a missed pregame freeze is never backfilled, and each shadow's own freeze
gate decides whether the injury evidence is good enough.
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import _bootstrap  # noqa: F401

from app.config import DATA_DIR, get_settings
from app.db.connection import LOCK_WAIT_ENV

SCRIPTS = Path(__file__).resolve().parent
BACKEND = _bootstrap.BACKEND
LOG_DIR = DATA_DIR / "logs"
STEPS = ("sync_espn.py", "sync_nfl.py", "compute_analytics.py")
PUBLISH = "publish_motherduck.py"
PUBLISH_SNAPSHOT = "publish_snapshot.py"   # replaces PUBLISH when R2 is configured (free tiers; MotherDuck retired)
PUBLISH_LEAGUES = "publish_leagues.py"     # also, when LEAGUE_PUBLISH_ID + FI_SYNC_DB_URL are set (multi-league path)
PUBLISHERS = (PUBLISH, PUBLISH_SNAPSHOT, PUBLISH_LEAGUES)
REPORT = "projection_performance.py"   # not in STEPS: run by run_report, whose failure never stops the chain
REPLAY = "model_replay.py"             # not in STEPS: run by run_replay after the report, never stops the chain
RANGES = "projection_ranges.py"         # not in STEPS: run by run_ranges before the publish, never stops the chain
EVIDENCE = "Evidence:"
TIMED_OUT = -999                     # run_step's result for a step killed at its deadline
EVIDENCE_TIMEOUT_S = 15 * 60         # per evidence step; REFRESH_EVIDENCE_TIMEOUT_S overrides
START_SIT_NOTHING_ELIGIBLE = 2       # start_sit_evaluation record: no pregame roster row to freeze
CTX_SHADOW_WARNINGS = 3              # ctx_shadow.py: ran and printed missing/stale injury-evidence WARNING lines
SYNCS = ("sync_espn.py", "sync_nfl.py")  # separate sources: one failing never stops the other
LOCK_WAIT_S = 120                    # FANTASY_DB_LOCK_WAIT_S for every step, unless the environment sets it
LOCK_RETRY_DELAYS_S = (60, 180)      # a step that still failed on the DuckDB lock runs again after each delay
LOCK_MARKERS = ("DatabaseBusy", "Could not set lock", "Conflicting lock")
SHADOW_STEP = " pregame week "       # a research shadow's evidence step is labelled "<name> pregame week W" or its
                                     # name ends in "_shadow" ("ipm_cal_shadow cycle"): those also run after a failed sync
RESEARCH_SHADOW_LABELS = ("ctx_shadow", "qb_scenario_shadow", "ipm_cal_shadow", "signal_shadows",
                          "injury_return_shadow")   # exit 3 = the shadow's own warnings
DST_SHADOW_WARNINGS = 3              # dst_shadow.py: ran and printed missing-input WARNING lines
DIST_LEDGER_WARNINGS = 3             # distribution_ledger.py: ran and printed WARNING lines (no artifact, missed week)


def plan(publish: bool) -> list[str]:
    """Step scripts in run order. Publishing needs R2 settings (the snapshot, preferred) or a read/write
    MotherDuck token, which only the hosted setup has, so a plain local install never pushes anywhere."""
    if not publish:
        return list(STEPS)
    if get_settings().r2_configured:
        leagues = bool((os.getenv("LEAGUE_PUBLISH_ID") or "").strip() and (os.getenv("FI_SYNC_DB_URL") or "").strip())
        # CLASSIC_SITE_ENABLED=0: the single-league site is retired, so its snapshot is no longer published
        classic = (os.getenv("CLASSIC_SITE_ENABLED") or "1").strip() != "0"
        return [*STEPS, *([PUBLISH_SNAPSHOT] if classic else []), *([PUBLISH_LEAGUES] if leagues else [])]
    has_token = bool((os.getenv("MOTHERDUCK_TOKEN") or "").strip())
    return [*STEPS, PUBLISH] if has_token else list(STEPS)


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self._f = path, path.open("a", encoding="utf-8")
        self.lines = 0
        self._recent: list[str] = []       # the last lines, for run_db_step's lock check

    def __call__(self, line: str = "") -> None:
        print(line, flush=True)
        self.lines += 1
        self._recent = [*self._recent[-199:], line]
        self._f.write(line + "\n")
        self._f.flush()

    def since(self, mark: int) -> list[str]:
        """The lines logged after `mark` (an earlier `lines` value), up to the last 200."""
        return self._recent[-min(self.lines - mark, len(self._recent)):] if self.lines > mark else []

    def close(self) -> None:
        self._f.close()


def run_step(cmd: list[str], log: Log, timeout: float | None = None) -> int:
    """Run one step, logging its output. `timeout` (seconds, evidence steps only) is a hard deadline: the
    step's whole process group is killed and TIMED_OUT returned. Without it the step may run as long as it
    needs (the syncs)."""
    started = time.monotonic()
    log(f"==> {Path(cmd[1]).name}  ({datetime.now().astimezone():%Y-%m-%d %H:%M %Z})")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    env.setdefault(LOCK_WAIT_ENV, str(LOCK_WAIT_S))
    if timeout is None:
        with subprocess.Popen(cmd, cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True) as proc:
            for line in proc.stdout:
                log(line.rstrip("\n"))
        log(f"<== exit {proc.returncode} after {time.monotonic() - started:.0f}s")
        return proc.returncode
    # own session, so the kill reaches anything the step spawned and closes the output pipe
    with subprocess.Popen(cmd, cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True) as proc:
        def pump() -> None:
            for line in proc.stdout:
                log(line.rstrip("\n"))

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.kill()
            proc.wait()
        reader.join(5)
    if timed_out:
        log(f"<== killed: no exit after {timeout:g}s")
        return TIMED_OUT
    log(f"<== exit {proc.returncode} after {time.monotonic() - started:.0f}s")
    return proc.returncode


def lock_failure(lines: list[str]) -> bool:
    return any(m in line for line in lines for m in LOCK_MARKERS)


def run_db_step(cmd: list[str], log: Log) -> int:
    """run_step for a step that writes the database: when it failed only because another process held the DuckDB lock
    (past the FANTASY_DB_LOCK_WAIT_S wait inside the step), run it again after each LOCK_RETRY_DELAYS_S delay."""
    name = Path(cmd[1]).name
    for attempt in range(len(LOCK_RETRY_DELAYS_S) + 1):
        mark = log.lines
        code = run_step(cmd, log)
        if code == 0 or not lock_failure(log.since(mark)):
            return code
        if attempt == len(LOCK_RETRY_DELAYS_S):
            log(f"WARNING: {name} still could not take the DuckDB lock after {attempt + 1} attempts")
            return code
        delay = LOCK_RETRY_DELAYS_S[attempt]
        log(f"WARNING: {name} could not take the DuckDB lock (another process holds it); "
            f"retry {attempt + 1}/{len(LOCK_RETRY_DELAYS_S)} in {delay:g}s")
        time.sleep(delay)
    return code


def pregame_warning(snap: dict, now: datetime) -> str | None:
    """Why this week's ESPN baseline is missing, or None when a pregame snapshot exists.
    `snap` is promotion_tracker.pregame_snapshot()."""
    week = snap["week"]
    if not snap["first_kickoff"]:
        return f"week {week} schedule has no kickoff times, so the pregame ESPN snapshot can't be checked"
    if snap["captured"]:
        return None
    first = datetime.fromisoformat(snap["first_kickoff"])
    latest = snap["latest_snapshot_at"] or "never"
    if now < first:
        return (f"no ESPN snapshot of week {week} projections yet (latest {latest}); "
                f"one must land before the first kickoff at {first.isoformat()}")
    return (f"missed the week {week} pregame ESPN snapshot: the latest ({latest}) is after the first "
            f"kickoff at {first.isoformat()}, so games that had started have no pregame ESPN baseline")


def check_pregame(log: Log, now: datetime | None = None) -> str | None:
    from app.analytics.promotion_tracker import pregame_snapshot
    from app.db.connection import open_db, table_exists

    settings = get_settings()
    with open_db(settings.db_path, read_only=True) as con:
        if not table_exists(con, "leagues"):
            msg = "no ESPN league synced yet, so the pregame ESPN snapshot can't be checked"
        else:
            lg = con.execute("SELECT season, current_week FROM leagues ORDER BY synced_at DESC LIMIT 1").fetchone()
            snap = pregame_snapshot(con, lg[0], lg[1])
            msg = pregame_warning(snap, now or datetime.now(timezone.utc))
            if msg is None:
                log(f"Pregame check: week {snap['week']} ESPN snapshot captured at "
                    f"{snap['latest_pregame_snapshot_at']} ({snap['players']} players), first kickoff "
                    f"{snap['first_kickoff']}.")
    if msg:
        log(f"WARNING: {msg}")
    return msg


# ----------------------------------------------------------------------------- evidence stage


def week_kickoffs(con, season: int) -> dict[int, list[datetime]]:
    """{week: known kickoffs (aware UTC)} of the REG schedule. A game whose clock is ambiguous or
    missing has no kickoff here (injury_freshness.scheduled_games never guesses one)."""
    from app.analytics.injury_freshness import scheduled_games
    from app.db.connection import table_exists

    if not table_exists(con, "nfl_schedules"):
        return {}
    weeks = [r[0] for r in con.execute(
        "SELECT DISTINCT week FROM nfl_schedules WHERE season = ? AND game_type = 'REG' ORDER BY week",
        [season]).fetchall()]
    return {int(w): sorted({g["kickoff"] for g in scheduled_games(con, season, w) if g["kickoff"]}) for w in weeks}


def pick_evidence_weeks(kickoffs: dict[int, list[datetime]], now: datetime) -> tuple[int | None, list[int]]:
    """(upcoming week, weeks whose games have all kicked off, newest first).

    The upcoming week is the earliest one with a game still to kick off, so it stays the same on the
    Thu, Sun and Mon runs of one NFL week (start_sit_evaluation refuses the players already started)
    and becomes the next week on Tuesday. A week with no known kickoff is in neither result."""
    last = {w: max(ks) for w, ks in kickoffs.items() if ks}
    ahead = [w for w, k in last.items() if k > now]
    done = sorted((w for w, k in last.items() if k <= now), reverse=True)
    return (min(ahead) if ahead else None), done


def week_is_final(con, season: int, week: int, cache: dict | None = None) -> tuple[bool, str]:
    """(fully final?, why not): weekly_learning.week_finality, the rule espn_benchmark counts weeks by.
    Pass the same `cache` dict for every week of one stage to load the season's sources only once."""
    from app.analytics import weekly_learning as wl
    from app.analytics.forecast_features import load_sources
    from app.analytics.independent_projection_store import league_scoring
    from app.analytics.schedule_intelligence import nfl_fetch_times

    cache = {} if cache is None else cache
    if "inputs" not in cache:
        _, scoring = league_scoring(con, season)
        scoring.require_supported()
        cache["inputs"] = (load_sources(con, scoring, [season]), nfl_fetch_times(con, season)["schedules"])
    sources, fetched_at = cache["inputs"]
    fin = wl.week_finality(sources, season, week, fetched_at)
    if fin["fully_final"]:
        return True, ""
    return False, (f"{fin['final_teams']}/{fin['scheduled_teams']} teams final, "
                   f"{len(fin['missing_ingestion_teams'])} without synced box scores or snaps")


def select_evidence(con, season: int, now: datetime, final: Callable | None = None) -> dict:
    """What the evidence stage runs, from a read-only connection:
    {season, week (upcoming, or None), final_week (latest fully final week, or None), notes}."""
    upcoming, done = pick_evidence_weeks(week_kickoffs(con, season), now)
    notes = []
    if upcoming is None:
        notes.append(f"no {season} regular-season week has a kickoff ahead; injury report and start/sit skipped")
    final_week = None
    if final is None:
        cache: dict = {}                 # the sources are loaded once, however many weeks are walked back
        final = lambda c, s, w: week_is_final(c, s, w, cache)  # noqa: E731
    for w in done:                       # newest first; stop at the first fully final week
        try:
            ok, why = final(con, season, w)
        except Exception as e:           # e.g. unsupported scoring: this week can't be judged final
            ok, why = False, f"finality check failed: {e!r}"
        if ok:
            final_week = w
            break
        notes.append(f"week {w} is not fully final ({why}); the ESPN benchmark waits for it")
    if final_week is None and not done:
        notes.append("no week has fully kicked off yet; ESPN benchmark skipped")
    return {"season": season, "week": upcoming, "final_week": final_week, "notes": notes}


def is_shadow(label: str) -> bool:
    """A research shadow's evidence step: labelled "<name> pregame week W", or its name ends in "_shadow"."""
    return SHADOW_STEP in label or label.split(" ", 1)[0].endswith("_shadow")


def evidence_steps(sel: dict) -> list[tuple[str, list[str]]]:
    """(label, command) in run order. The scripts open the database read-only and write only their
    git-ignored default output dirs (start_sit_evaluation, espn_benchmark, ctx_shadow under
    data/models/research/context, dst_shadow under data/models/dst; injury_freshness `report` writes nothing). A research shadow's evidence step is
    labelled "<name> pregame week W" or named "<name>_shadow ..." (is_shadow): those steps also run after a failed sync."""
    py, season = sys.executable, str(sel["season"])
    steps = []
    if sel["week"] is not None:
        week = str(sel["week"])
        steps.append((f"injury_freshness week {week}",
                      [py, str(SCRIPTS / "injury_freshness.py"), "report", "--season", season, "--week", week]))
        steps.append((f"start_sit_evaluation week {week}",
                      [py, str(SCRIPTS / "start_sit_evaluation.py"), "record", "--season", season, "--week", week]))
        # research shadow (ifp-ctx-p1): gated write-once pregame ledger under data/models/research/context/
        steps.append((f"ctx_shadow pregame week {week}",
                      [py, str(SCRIPTS / "ctx_shadow.py"), "pregame", "--season", season, "--week", week]))
        # research shadow (ifp-ctx-p2q, QB only): the same gated freeze under data/models/research/qb_scenario_shadow/
        steps.append((f"qb_scenario_shadow pregame week {week}",
                      [py, str(SCRIPTS / "qb_scenario_shadow.py"), "pregame", "--season", season, "--week", week]))
        # research shadows (ifp-fr-p1 / ifp-dct-p1 / ifp-awd-p1): write-once ledger under data/models/research/signal_shadows/
        steps.append((f"signal_shadows pregame week {week}",
                      [py, str(SCRIPTS / "signal_shadows.py"), "pregame", "--season", season, "--week", week]))
        # research shadow (ifp-irs-p1 / ifp-irb-p1): write-once ledger under data/models/research/injury_return_shadow/
        steps.append((f"injury_return_shadow pregame week {week}",
                      [py, str(SCRIPTS / "injury_return_shadow.py"), "pregame", "--season", season, "--week", week]))
        # research shadow (dfp-r1 D/ST): gated write-once pregame ledger under data/models/dst/shadow/
        steps.append((f"dst_shadow pregame week {week}",
                      [py, str(SCRIPTS / "dst_shadow.py"), "pregame", "--season", season, "--week", week]))
        # research (ifpd): write-once pregame distribution ledger under data/models/research/point_distributions/
        steps.append((f"distribution_ledger pregame week {week}",
                      [py, str(SCRIPTS / "distribution_ledger.py"), "freeze", "--season", season, "--week", week]))
    # research re-test of the QB participation map: freeze once, score fully final weeks (research/participation_cal_shadow/)
    steps.append(("ipm_cal_shadow cycle", [py, str(SCRIPTS / "participation_cal_shadow.py"), "cycle", "--season", season]))
    if sel["final_week"] is not None:
        week = str(sel["final_week"])
        steps.append((f"espn_benchmark week {week}",
                      [py, str(SCRIPTS / "espn_benchmark.py"), "record", "--season", season, "--week", week]))
    return steps


def evidence_timeout() -> float:
    try:
        return float(os.getenv("REFRESH_EVIDENCE_TIMEOUT_S") or EVIDENCE_TIMEOUT_S)
    except ValueError:
        return EVIDENCE_TIMEOUT_S


def run_evidence(log: Log, now: datetime | None = None, timeout: float | None = None,
                 shadows_only: bool = False) -> list[str]:
    """Run the evidence stage; returns its problems (each also logged as a WARNING). Never raises.
    Each step gets `timeout` seconds (default 15 min) and is killed after that, so a stuck step cannot
    keep the refresh running into the next scheduled launch. `shadows_only`: only the research shadows' pregame
    freezes (what still runs after a failed sync)."""
    problems: list[str] = []
    timeout = evidence_timeout() if timeout is None else timeout

    def warn(msg: str) -> None:
        problems.append(msg)
        log(f"WARNING: {EVIDENCE} {msg}")

    log(f"==> {EVIDENCE} " + ("research shadows only, on the data already stored (a sync failed)" if shadows_only else
                              "read-only stage (injury freshness, start/sit decision set, research shadows, "
                              "ESPN benchmark)"))
    try:
        from app.db.connection import open_db, table_exists

        settings = get_settings()
        with open_db(settings.db_path, read_only=True) as con:    # closed before any step opens its own
            season = settings.nfl_season
            if table_exists(con, "leagues"):
                row = con.execute("SELECT season FROM leagues ORDER BY synced_at DESC LIMIT 1").fetchone()
                season = int(row[0]) if row else season
            sel = select_evidence(con, season, now or datetime.now(timezone.utc))
        steps = [s for s in evidence_steps(sel) if is_shadow(s[0]) or not shadows_only]
    except Exception as e:
        warn(f"could not choose the season and week: {e!r}")
        return problems
    log(f"{EVIDENCE} season {sel['season']}, upcoming week {sel['week']}, latest fully final week {sel['final_week']}")
    for note in sel["notes"]:
        log(f"{EVIDENCE} {note}")
    for label, cmd in steps:
        try:
            code = run_step(cmd, log, timeout=timeout)
        except Exception as e:
            warn(f"{label} could not run: {e!r}")
            continue
        if code == TIMED_OUT:
            warn(f"{label} timed out after {timeout:g}s and was killed")
        elif code == START_SIT_NOTHING_ELIGIBLE and label.startswith("start_sit_evaluation"):
            log(f"INFO: {EVIDENCE} {label} recorded nothing: no roster player is still pregame and synced")
        elif code == CTX_SHADOW_WARNINGS and label.startswith(RESEARCH_SHADOW_LABELS):
            # research shadow only: its injury-evidence warnings are its own WARNING lines above and its run record,
            # never an evidence-stage problem (evidence health must not report a production capture as failed)
            what = "injury-evidence warnings" if " pregame " in label else "warnings"
            log(f"INFO: {EVIDENCE} {label} reported {what} for the research shadow (see above)")
        elif code == DST_SHADOW_WARNINGS and label.startswith("dst_shadow"):
            log(f"INFO: {EVIDENCE} {label} reported missing-input warnings for the D/ST research shadow (see above)")
        elif code == DIST_LEDGER_WARNINGS and label.startswith("distribution_ledger"):
            log(f"INFO: {EVIDENCE} {label} reported warnings for its research ledger (see above)")
        elif code != 0:
            warn(f"{label} exited {code} (output above; the refresh exit code is unchanged)")
    log(f"{EVIDENCE} " + (f"{len(problems)} problem(s), see the WARNING lines above" if problems else "all steps ok"))
    return problems


def run_shadows(log: Log) -> None:
    """The research shadows' pregame freezes after a failed sync. Never raises; never changes the exit code."""
    try:
        run_evidence(log, shadows_only=True)
    except Exception as e:  # run_evidence handles its own errors; this is the last guard
        log(f"WARNING: {EVIDENCE} research shadows failed: {e!r}")


def run_report(log: Log, timeout: float | None = None) -> str | None:
    """Build and store the projection-performance report (one write-once row). Returns the problem, if any
    (also logged as a WARNING). Never raises: the data is refreshed whatever happens here."""
    timeout = evidence_timeout() if timeout is None else timeout
    try:
        code = run_step([sys.executable, str(SCRIPTS / REPORT), "publish"], log, timeout=timeout)
    except Exception as e:
        problem = f"{REPORT} could not run: {e!r}"
    else:
        if code == 0:
            return None
        problem = (f"{REPORT} timed out after {timeout:g}s and was killed" if code == TIMED_OUT else
                   f"{REPORT} exited {code}; the previous report stays published")
    log(f"WARNING: report: {problem}")
    return problem


def run_replay(log: Log, timeout: float | None = None) -> str | None:
    """Replay the production model on every fully final week and store a snapshot when the model, its inputs or its
    forecasts changed (model_replay.py publish, one write-once row). Returns the problem, if any (logged as a WARNING).
    Never raises. It runs BEFORE the MotherDuck publish on purpose: the publish then carries this refresh's replay, at
    the cost of its runtime (~1-2 min, the cached store) on the hosted data; after the publish the hosted replay would
    always be one refresh behind, which matters right after a model change. --skip-replay leaves it out."""
    timeout = evidence_timeout() if timeout is None else timeout
    try:
        code = run_step([sys.executable, str(SCRIPTS / REPLAY), "publish"], log, timeout=timeout)
    except Exception as e:
        problem = f"{REPLAY} could not run: {e!r}"
    else:
        if code == 0:
            return None
        problem = (f"{REPLAY} timed out after {timeout:g}s and was killed" if code == TIMED_OUT else
                   f"{REPLAY} exited {code}; the previous replay stays published")
    log(f"WARNING: replay: {problem}")
    return problem


def run_ranges(log: Log) -> str | None:
    """Copy the pinned point-distribution table into the database (projection_ranges.py load, a no-op once the pin is
    loaded) so the publish carries the floor / ceiling ranges. Returns the problem, if any (logged as a WARNING).
    Never raises; exit 3 (no pin yet) is informational."""
    try:
        code = run_db_step([sys.executable, str(SCRIPTS / RANGES), "load"], log)
    except Exception as e:
        problem = f"{RANGES} could not run: {e!r}"
    else:
        if code in (0, 3):
            return None
        problem = f"{RANGES} exited {code}; the previously loaded ranges stay served"
    log(f"WARNING: ranges: {problem}")
    return problem


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-publish", action="store_true", help="skip MotherDuck even if MOTHERDUCK_TOKEN is set")
    parser.add_argument("--check-only", action="store_true", help="only run the pregame snapshot check")
    parser.add_argument("--skip-evidence", action="store_true",
                        help="skip the read-only evidence stage (injury report, start/sit record, ESPN benchmark)")
    parser.add_argument("--skip-report", action="store_true",
                        help="skip building the projection-performance report before the publish")
    parser.add_argument("--skip-replay", action="store_true",
                        help="skip the weekly model replay (model_replay.py publish) before the publish")
    args = parser.parse_args()

    log = Log(LOG_DIR / f"refresh-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log")
    try:
        if not args.check_only:
            steps = plan(publish=not args.no_publish)
            publishing = any(s in PUBLISHERS for s in steps)
            if not publishing:
                log("Hosted publish skipped (" + ("--no-publish" if args.no_publish
                                                  else "neither R2 snapshot settings nor MOTHERDUCK_TOKEN set") + ")")
            failed: list[str] = []
            for step in steps:
                if failed and step not in SYNCS:
                    log(f"SKIPPED: {step} (needs {', '.join(failed)}, which failed)")
                    continue
                if step in PUBLISHERS and not args.skip_report:
                    run_report(log)          # after analytics, so the publish carries the new report
                if step in PUBLISHERS and not args.skip_replay:
                    run_replay(log)          # and the newest weekly model replay
                if step in PUBLISHERS:
                    run_ranges(log)          # so the publish carries the pinned floor / ceiling table
                code = run_db_step([sys.executable, str(SCRIPTS / step)], log)
                if code != 0:
                    if step not in PUBLISHERS:
                        log(f"FAILED: {step} exited {code}; the steps that need it are not run. Log: {log.path}")
                        failed.append(step)
                        continue
                    log(f"FAILED: {step} exited {code}; later steps were not run. Log: {log.path}")
                    # Publishing is a network step unrelated to the local data, which is fully refreshed:
                    # still capture the pregame evidence (lost for games that kick off before the next run).
                    if not args.skip_evidence:
                        try:
                            run_evidence(log)
                        except Exception as e:
                            log(f"WARNING: {EVIDENCE} stage failed: {e!r}")
                    return 1
            if failed:
                # A pregame freeze missed now is never backfilled: the research shadows still run on the stored data
                # (each one's freeze gate checks the injury evidence); the rest of the evidence stage waits.
                if not args.skip_evidence:
                    run_shadows(log)
                return 1
            if not publishing and not args.skip_report:
                run_report(log)              # local install: the local API serves the report
            if not publishing and not args.skip_replay:
                run_replay(log)
            if not publishing:
                run_ranges(log)
        try:
            check_pregame(log)
        except Exception as e:  # the data is refreshed; a broken check must still be visible
            log(f"WARNING: pregame snapshot check failed: {e!r}")
        if not (args.check_only or args.skip_evidence):
            try:
                run_evidence(log)
            except Exception as e:  # run_evidence handles its own errors; this is the last guard
                log(f"WARNING: {EVIDENCE} stage failed: {e!r}")
        log(f"Refresh complete. Log: {log.path}")
        return 0
    finally:
        log.close()


def keep_awake() -> None:
    """macOS: hold an idle-sleep (and, on power, system-sleep) assertion until this process exits, so a run started
    on a scheduled wake, on login or by the guard is not cut short when the Mac dozes off again. No-op elsewhere."""
    exe = shutil.which("caffeinate")
    if exe:
        subprocess.Popen([exe, "-i", "-s", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    keep_awake()
    sys.exit(main())

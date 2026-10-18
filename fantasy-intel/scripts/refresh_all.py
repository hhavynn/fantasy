"""Full data refresh: ESPN -> nflverse -> analytics -> projection-performance report -> MotherDuck (only
when MOTHERDUCK_TOKEN is set), then a check that this week's ESPN projections were snapshotted before the
first kickoff, then the read-only "evidence" stage (injury freshness report, start/sit decision set, ESPN
benchmark). launchd runs it on a weekly schedule (see README "Scheduled refresh"); it is also fine by hand:

    cd backend && uv run python ../scripts/refresh_all.py [--no-publish] [--check-only] [--skip-evidence] [--skip-report]

Report step (docs/projection_performance.md): after the analytics step, scripts/projection_performance.py
publish builds the accuracy scorecard from a read-only connection and inserts it as one write-once row, so the
MotherDuck publish that follows carries it to the hosted site. It can never fail the refresh: a problem is
logged as a WARNING and the publish still runs.

Each step runs in its own process and the first failure stops the chain (exit 1). Every run writes
data/logs/refresh-<UTC stamp>.log with each step's output.

Evidence stage (docs/independent_projection_engine.md, "Weekly evidence routine (automated)"): it runs
only after every sync and analytics step has exited, so no write connection is open; it opens the
database read-only (here and in the four scripts it launches) and runs no sync. It can never fail the
refresh: each step's problem is logged as a WARNING and the exit code is unchanged. One of them is the
research shadow of the frozen ifp-ctx-p1 candidate (scripts/ctx_shadow.py pregame): it freezes that week's
write-once pregame ledger once the final injury reports are in, or the main slate is within 6 hours. Another is
scripts/signal_shadows.py pregame (ifp-fr-p1 / ifp-dct-p1 / ifp-awd-p1): it freezes its write-once ledger once the main
slate is within 6 hours, after re-fetching the season's FTN, play-by-play and depth-chart files from nflverse.
"""

import argparse
import os
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

SCRIPTS = Path(__file__).resolve().parent
BACKEND = _bootstrap.BACKEND
LOG_DIR = DATA_DIR / "logs"
STEPS = ("sync_espn.py", "sync_nfl.py", "compute_analytics.py")
PUBLISH = "publish_motherduck.py"
REPORT = "projection_performance.py"   # not in STEPS: run by run_report, whose failure never stops the chain
EVIDENCE = "Evidence:"
TIMED_OUT = -999                     # run_step's result for a step killed at its deadline
EVIDENCE_TIMEOUT_S = 15 * 60         # per evidence step; REFRESH_EVIDENCE_TIMEOUT_S overrides
START_SIT_NOTHING_ELIGIBLE = 2       # start_sit_evaluation record: no pregame roster row to freeze
CTX_SHADOW_WARNINGS = 3              # ctx_shadow.py / signal_shadows.py: ran and printed their own WARNING lines


def plan(publish: bool) -> list[str]:
    """Step scripts in run order. Publishing needs a read/write MotherDuck token, which only the
    hosted setup has, so a plain local install never pushes anywhere."""
    has_token = bool((os.getenv("MOTHERDUCK_TOKEN") or "").strip())
    return [*STEPS, PUBLISH] if publish and has_token else list(STEPS)


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self._f = path, path.open("a", encoding="utf-8")

    def __call__(self, line: str = "") -> None:
        print(line, flush=True)
        self._f.write(line + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def run_step(cmd: list[str], log: Log, timeout: float | None = None) -> int:
    """Run one step, logging its output. `timeout` (seconds, evidence steps only) is a hard deadline: the
    step's whole process group is killed and TIMED_OUT returned. Without it the step may run as long as it
    needs (the syncs)."""
    started = time.monotonic()
    log(f"==> {Path(cmd[1]).name}  ({datetime.now().astimezone():%Y-%m-%d %H:%M %Z})")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
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


def evidence_steps(sel: dict) -> list[tuple[str, list[str]]]:
    """(label, command) in run order. The scripts open the database read-only and write only their
    git-ignored default output dirs (start_sit_evaluation, espn_benchmark, ctx_shadow under
    data/models/research/context, signal_shadows under data/models/research/signal_shadows; injury_freshness `report`
    writes nothing)."""
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
        # research shadows (ifp-fr-p1 / ifp-dct-p1 / ifp-awd-p1): write-once ledger under data/models/research/signal_shadows/
        steps.append((f"signal_shadows pregame week {week}",
                      [py, str(SCRIPTS / "signal_shadows.py"), "pregame", "--season", season, "--week", week]))
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


def run_evidence(log: Log, now: datetime | None = None, timeout: float | None = None) -> list[str]:
    """Run the evidence stage; returns its problems (each also logged as a WARNING). Never raises.
    Each step gets `timeout` seconds (default 15 min) and is killed after that, so a stuck step cannot
    keep the refresh running into the next scheduled launch."""
    problems: list[str] = []
    timeout = evidence_timeout() if timeout is None else timeout

    def warn(msg: str) -> None:
        problems.append(msg)
        log(f"WARNING: {EVIDENCE} {msg}")

    log(f"==> {EVIDENCE} read-only stage (injury freshness, start/sit decision set, "
        "ifp-ctx-p1 shadow, signal shadows, ESPN benchmark)")
    try:
        from app.db.connection import open_db, table_exists

        settings = get_settings()
        with open_db(settings.db_path, read_only=True) as con:    # closed before any step opens its own
            season = settings.nfl_season
            if table_exists(con, "leagues"):
                row = con.execute("SELECT season FROM leagues ORDER BY synced_at DESC LIMIT 1").fetchone()
                season = int(row[0]) if row else season
            sel = select_evidence(con, season, now or datetime.now(timezone.utc))
        steps = evidence_steps(sel)
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
        elif code == CTX_SHADOW_WARNINGS and label.startswith(("ctx_shadow", "signal_shadows")):
            # research shadow only: its injury-evidence warnings are its own WARNING lines above and its run record,
            # never an evidence-stage problem (evidence health must not report a production capture as failed)
            log(f"INFO: {EVIDENCE} {label} reported "
                + ("injury-evidence " if label.startswith("ctx_shadow") else "") + "warnings for the research shadow (see above)")
        elif code != 0:
            warn(f"{label} exited {code} (output above; the refresh exit code is unchanged)")
    log(f"{EVIDENCE} " + (f"{len(problems)} problem(s), see the WARNING lines above" if problems else "all steps ok"))
    return problems


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-publish", action="store_true", help="skip MotherDuck even if MOTHERDUCK_TOKEN is set")
    parser.add_argument("--check-only", action="store_true", help="only run the pregame snapshot check")
    parser.add_argument("--skip-evidence", action="store_true",
                        help="skip the read-only evidence stage (injury report, start/sit record, ESPN benchmark)")
    parser.add_argument("--skip-report", action="store_true",
                        help="skip building the projection-performance report before the publish")
    args = parser.parse_args()

    log = Log(LOG_DIR / f"refresh-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log")
    try:
        if not args.check_only:
            steps = plan(publish=not args.no_publish)
            if PUBLISH not in steps:
                log("MotherDuck publish skipped (" + ("--no-publish" if args.no_publish else "MOTHERDUCK_TOKEN not set") + ")")
            for step in steps:
                if step == PUBLISH and not args.skip_report:
                    run_report(log)          # after analytics, so the publish carries the new report
                code = run_step([sys.executable, str(SCRIPTS / step)], log)
                if code != 0:
                    log(f"FAILED: {step} exited {code}; later steps were not run. Log: {log.path}")
                    if step != PUBLISH:
                        return 1
                    # Publishing is a network step unrelated to the local data, which is fully refreshed:
                    # still capture the pregame evidence (lost for games that kick off before the next run).
                    if not args.skip_evidence:
                        try:
                            run_evidence(log)
                        except Exception as e:
                            log(f"WARNING: {EVIDENCE} stage failed: {e!r}")
                    return 1
            if PUBLISH not in steps and not args.skip_report:
                run_report(log)              # local install: the local API serves the report
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


if __name__ == "__main__":
    sys.exit(main())

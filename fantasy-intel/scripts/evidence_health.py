"""Is real prospective evidence being collected on this Mac? One read-only answer (docs/evidence_health.md).

    cd backend
    uv run python ../scripts/evidence_health.py                      # compact summary (status is the default)
    uv run python ../scripts/evidence_health.py status --json        # everything, machine-readable
    uv run python ../scripts/evidence_health.py status --hosted      # also read MotherDuck's schema/publish stamp
    uv run python ../scripts/evidence_health.py status --notify      # macOS notification for NEW warn/fail issues
    uv run python ../scripts/evidence_health.py guard                # pregame readiness: what would the guard do?
    uv run python ../scripts/evidence_health.py guard --execute      # ... and run refresh_all.py if a capture is due
    uv run python ../scripts/evidence_health.py launchd verify       # are the launchd jobs installed as templated?
    uv run python ../scripts/evidence_health.py launchd install      # idempotent install PLAN (prints commands)
    uv run python ../scripts/evidence_health.py launchd install --apply [--jobs refresh,weekly-learning,pregame-guard]

`status` exits 0 (ok/info), 2 (warnings) or 1 (failures). It never syncs, trains, publishes or writes a database:
it reads the live DuckDB read-only (a copy when a refresh holds the write lock), the refresh logs, the evidence
files and launchd. A zero exit of refresh_all.py is not taken as evidence: each evidence substep is checked.

`guard` is the schedule-aware pregame check (launchd template com.fantasy-intel.pregame-guard.plist runs it every
30 min). It reads the kickoff times in nfl_schedules and runs refresh_all.py only when the next kickoff is within
--lead-hours and no clean capture landed within --max-age-hours of it; at most --max-attempts per kickoff, spaced
by --retry-minutes, and never after kickoff (a missed capture is reported, never fabricated).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

import evidence_audit as eh
from app.config import REPO_ROOT, get_settings
from app.db.connection import DatabaseBusy, connect

SCRIPTS = Path(__file__).resolve().parent
BACKEND = _bootstrap.BACKEND
REFRESH_TIMEOUT_S = 45 * 60
EXIT = {eh.OK: 0, eh.INFO: 0, eh.WARN: 2, eh.FAIL: 1}


def _now(args) -> datetime:
    return datetime.fromisoformat(args.now).astimezone(timezone.utc) if getattr(args, "now", None) else datetime.now(timezone.utc)


def _paths(args) -> eh.Paths:
    root = Path(args.root) if getattr(args, "root", None) else REPO_ROOT
    db = Path(args.db) if getattr(args, "db", None) else get_settings().db_path
    return eh.default_paths(root, db)


def _season(args, con) -> int:
    if args.season:
        return args.season
    if con is not None:
        try:
            row = con.execute("SELECT season FROM leagues ORDER BY synced_at DESC LIMIT 1").fetchone()
            if row:
                return int(row[0])
        except Exception:
            pass
    return get_settings().nfl_season


@contextmanager
def open_live(db: Path):
    """Read-only connection to the live DB. When a refresh holds DuckDB's write lock, read a temporary copy
    (the note says so); yields (con or None, note)."""
    if not db.exists():
        yield None, f"{db} does not exist"
        return
    con, note, tmp = None, None, None
    try:
        con = connect(db, read_only=True, retries=8)
    except DatabaseBusy:
        tmp = Path(tempfile.mkdtemp(prefix="evidence-health-"))
        for f in (db, db.with_name(db.name + ".wal")):
            if f.exists():
                shutil.copy2(f, tmp / f.name)
        try:
            con = connect(tmp / db.name, read_only=True, retries=1)
            note = "read from a copy taken while another process held the database write lock"
        except Exception as err:
            note = f"database busy and its copy unreadable: {err}"
    except Exception as err:
        note = f"could not open {db} read-only: {err.__class__.__name__}: {err}"
    try:
        yield con, note
    finally:
        if con is not None:
            con.close()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def build_report(args, now: datetime | None = None, runner=None) -> dict:
    paths = _paths(args)
    now = now or _now(args)
    with open_live(paths.db) as (con, note):
        hosted = None
        if args.hosted:
            from app.db.connection import connect_motherduck
            hosted = eh.hosted_status(get_settings().motherduck_database or args.database, connect_motherduck)
        ctx = eh.Context(now=now, season=_season(args, con), paths=paths, uv=shutil.which("uv") or "uv",
                         uid=os.getuid(), runner=runner or eh.run_command, from_week=args.from_week, hosted=hosted,
                         db_note=note)
        return eh.assess(con, ctx)


def notify(report: dict, paths: eh.Paths, now: datetime, runner=None) -> list[dict]:
    """Notify each warn/fail issue once while it stays open (state: data/logs/evidence_health/alerts.json).
    New fingerprints are stored only when the notification was actually delivered: after a failed `osascript`
    they stay unsent, so the next run tries again instead of silently suppressing the alert."""
    state_file = paths.state / "alerts.json"
    fresh, state = eh.new_alerts(report["issues"], eh.read_json(state_file, {}), now)
    if fresh:
        worst = max(fresh, key=lambda i: eh._RANK[i["severity"]])
        more = f" (+{len(fresh) - 1} more: evidence_health.py)" if len(fresh) > 1 else ""
        sent = eh.notify_macos(f"Fantasy evidence: {worst['severity'].upper()}", worst["message"] + more,
                               runner or eh.run_command)
        if not sent:
            for i in fresh:
                state.pop(i["fingerprint"], None)
            fresh = []
    eh.write_json_atomic(state_file, state)
    return fresh


def cmd_status(args) -> int:
    now = _now(args)
    report = build_report(args, now)
    if args.notify:
        report["notified"] = [i["fingerprint"] for i in notify(report, _paths(args), now)]
    print(json.dumps(report, indent=1, default=str) if args.json else eh.render(report))
    return EXIT[report["status"]]


# ----------------------------------------------------------------------------- guard


@contextmanager
def guard_lock(state_dir: Path):
    """One guard at a time; a lock left by a dead process is taken over."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / "guard.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or 0)
                os.kill(pid, 0)
            except (ValueError, ProcessLookupError, OSError):
                lock.unlink(missing_ok=True)          # stale
                continue
            yield False
            return
    else:
        yield False
        return
    try:
        yield True
    finally:
        lock.unlink(missing_ok=True)


def run_refresh(timeout: float = REFRESH_TIMEOUT_S) -> int:
    """refresh_all.py exactly as launchd runs it (its own log under data/logs)."""
    try:
        return subprocess.run([sys.executable, str(SCRIPTS / "refresh_all.py")], cwd=BACKEND, timeout=timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    except subprocess.TimeoutExpired:
        return -999


def cmd_guard(args, refresh=run_refresh, runner=None, sleep=time.sleep) -> int:
    now, paths, runner = _now(args), _paths(args), runner or eh.run_command
    cfg = eh.GuardConfig(lead_h=args.lead_hours, max_age_h=args.max_age_hours, max_attempts=args.max_attempts,
                         retry_min=args.retry_minutes)
    state_file = paths.state / "guard-state.json"
    with guard_lock(paths.state) as mine:
        if not mine:
            print(json.dumps({"action": "skip", "reason": "another guard is running"}))
            return 0
        with open_live(paths.db) as (con, note):
            if con is None:
                print(json.dumps({"action": "error", "reason": note}))
                return 1
            season = _season(args, con)
            kicks, espn = eh.espn_latest_by_slot(con, season, now)
        runs = eh.refresh_runs(paths.logs, now)
        # the loaded refresh job's calendar: the guard waits for a scheduled run that covers the kickoff
        job = eh.job_status(paths, "refresh", runner, _uv(), os.getuid())
        scheduled = eh.occurrences(job["_entries"], now - timedelta(hours=2), now + timedelta(days=2)) \
            if job["launchctl"].get("loaded") else []
        state = eh.read_json(state_file, {"attempts": {}})
        attempts = {k: v for k, v in state.get("attempts", {}).items()       # forget slots over a week old
                    if (t := eh.utc(k)) and t > now - timedelta(days=7)}
        d = eh.guard_decision(now, kicks, runs, espn, attempts, cfg, scheduled)
        d.update(season=season, checked_at=now.isoformat(), db_note=note)
        c, caught = None, {}
        if getattr(args, "catch_up", False):
            # a scheduled firing the Mac slept or was shut down through: run it now (on wake or login)
            ccfg = eh.CatchupConfig()
            caught = {k: v for k, v in state.get("catchup_attempts", {}).items()
                      if (t := eh.utc(k)) and t > now - timedelta(days=ccfg.lookback_d + 1)}
            fired = eh.occurrences(job["_entries"], now - timedelta(days=ccfg.lookback_d), now) \
                if job["launchctl"].get("loaded") else []
            c = eh.catchup_decision(now, fired, runs, caught, ccfg, job["launchctl"].get("state") == "running")
            if c["action"] == "run" and d["action"] != "run" and args.execute and args.catch_up_settle_seconds > 0:
                # on wake launchd replays the missed calendar firing too: give that refresh time to start, then
                # look again, so the guard never launches a second one beside it
                sleep(args.catch_up_settle_seconds)
                job = eh.job_status(paths, "refresh", runner, _uv(), os.getuid())
                runs = eh.refresh_runs(paths.logs, now)
                c = eh.catchup_decision(now, fired, runs, caught, ccfg, job["launchctl"].get("state") == "running")
            d["catchup"] = c
        due = d["action"] == "run" or (c is not None and c["action"] == "run")
        if due and args.execute:
            code = refresh()
            if d["action"] == "run":
                attempts.setdefault(d["slot"], []).append({"at": now.isoformat(), "exit": code})
            if c is not None and c["action"] == "run":
                caught.setdefault(c["slot"], []).append({"at": now.isoformat(), "exit": code})
            d.update(executed=True, refresh_exit=code)
        elif due:
            d["executed"] = False
            d["would_run"] = f"cd {BACKEND} && {sys.executable} ../scripts/refresh_all.py"
        state.update(attempts=attempts, last=d)
        if c is not None:
            state["catchup_attempts"] = caught
        eh.write_json_atomic(state_file, state)
    if args.notify:
        alerts = []
        if d["action"] == "gave_up":
            alerts.append({"fingerprint": f"guard_gave_up:{d['slot']}", "severity": eh.FAIL,
                           "message": f"pregame guard gave up before kickoff {d['slot']}: {d['reason']}"})
        c = d.get("catchup") or {}
        if c.get("action") == "gave_up":
            alerts.append({"fingerprint": f"catchup_gave_up:{c['slot']}", "severity": eh.FAIL,
                           "message": f"refresh catch-up gave up on the {c['slot']} firing: {c['reason']}"})
        if d.get("executed") and d.get("refresh_exit") != 0:
            slot = d["slot"] if d["action"] == "run" else f"catch-up {c.get('slot')}"
            alerts.append({"fingerprint": f"guard_refresh_failed:{slot}", "severity": eh.WARN,
                           "message": f"guard refresh for {'kickoff ' if d['action'] == 'run' else ''}{slot} "
                                      f"exited {d['refresh_exit']}"})
        report = build_report(args, now, runner)
        report["issues"] = report["issues"] + alerts
        d["notified"] = [i["fingerprint"] for i in notify(report, paths, now, runner)]
    print(json.dumps(d, indent=1, default=str))
    return 1 if d["action"] == "gave_up" or (d.get("catchup") or {}).get("action") == "gave_up" else 0


# ----------------------------------------------------------------------------- launchd


def _uv() -> str:
    return shutil.which("uv") or "uv"


def launchd_plan(paths: eh.Paths, jobs: list[str], uid: int, uv: str, runner, now: datetime | None = None,
                 repoint: bool = False) -> list[dict]:
    """Idempotent install plan per job: [{job, state, actions [shell commands], plist_text}]. Nothing is done for
    a job that is installed exactly as templated and loaded; a drifted plist is backed up before replacement. A job
    installed for ANOTHER checkout (its WorkingDirectory is not this one's backend/, e.g. when run from a git
    worktree) is left alone unless `repoint`: the job must keep running from the checkout that holds .env."""
    now = now or datetime.now(timezone.utc)
    here = str(paths.root / "backend")
    # where the already-installed fantasy-intel jobs run: a new job joins them, never a second checkout
    homes = {wd for j in eh.JOBS if (wd := (eh.read_plist(paths.launch_agents / f"{eh.label(j)}.plist") or {})
                                     .get("WorkingDirectory"))}
    out = []
    for job in jobs:
        st = eh.job_status(paths, job, runner, uv, uid)
        _, text = eh.expected_plist(paths, job, uv)
        target, gui = Path(st["plist"]), f"gui/{uid}"
        if text is None:
            out.append({"job": job, "state": "no_template", "actions": [], "plist_text": None})
            continue
        loaded = st["launchctl"].get("loaded")
        actions, state = [], None
        q = shlex.quote
        installed_wd = (eh.read_plist(target) or {}).get("WorkingDirectory") if st["installed"] else None
        elsewhere = ({installed_wd} if installed_wd else homes) - {here}
        if elsewhere and not repoint:
            out.append({"job": job, "state": "other_checkout", "drift": [
                f"fantasy-intel jobs on this Mac run from {', '.join(sorted(elsewhere))}; this is {here}: run this "
                "command from that checkout, or pass --repoint"], "actions": [], "plist_text": text})
            continue
        if not st["installed"]:
            state = "not_installed"
            actions = [f"mkdir -p {q(str(paths.logs))} {q(str(paths.launch_agents))}", f"WRITE {target}",
                       f"plutil -lint {q(str(target))}", f"launchctl bootstrap {gui} {q(str(target))}"]
        elif st["matches_template"] and loaded:
            state = "up_to_date"
        elif st["matches_template"]:
            state = "installed_not_loaded"
            actions = [f"launchctl bootstrap {gui} {q(str(target))}"]
        else:
            state = "drifted"
            backup = target.with_name(f"{target.name}.bak-{now:%Y%m%dT%H%M%SZ}")
            actions = [f"cp {q(str(target))} {q(str(backup))}"] + \
                      ([f"launchctl bootout {gui}/{st['label']}"] if loaded else []) + \
                      [f"WRITE {target}", f"plutil -lint {q(str(target))}", f"launchctl bootstrap {gui} {q(str(target))}"]
        out.append({"job": job, "state": state, "drift": st["drift"], "actions": actions, "plist_text": text})
    return out


def apply_plan(plan: list[dict], runner) -> list[dict]:
    """Execute a plan; stops a job at its first failing command."""
    results = []
    for p in plan:
        for a in p["actions"]:
            if a.startswith("WRITE "):
                path = Path(a[len("WRITE "):])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(p["plist_text"])
                code, out = 0, ""
            else:
                code, out = runner(shlex.split(a))
            results.append({"job": p["job"], "action": a, "exit": code, "output": out.strip()[-300:]})
            if code != 0:
                break
    return results


def cmd_launchd(args, runner=None) -> int:
    paths, uid, uv, runner = _paths(args), os.getuid(), _uv(), runner or eh.run_command
    jobs = [j.strip() for j in args.jobs.split(",") if j.strip()]
    bad = [j for j in jobs if j not in eh.JOBS]
    if bad:
        raise SystemExit(f"unknown job(s) {bad}; choose from {', '.join(eh.JOBS)}")
    if args.action == "verify":
        rows = [eh.job_status(paths, j, runner, uv, uid) for j in jobs]
        ok = True
        for r in rows:
            r.pop("_entries", None)
            lc = r["launchctl"]
            problems = ([] if r["installed"] else ["not installed"]) + (["invalid plist"] if r["valid"] is False else []) + \
                       ([f"template unreadable: {r['template_error']}"] if r.get("template_error") else []) + \
                       (["not loaded"] if r["installed"] and not lc.get("loaded") else []) + \
                       (["program missing"] if r["program_ok"] is False else []) + r["drift"]
            r["problems"] = problems
            ok &= not problems
            if not args.json:
                print(f"{r['job']:<16} {'OK' if not problems else 'PROBLEM'}: "
                      + ("; ".join(problems) if problems else f"loaded, runs {lc.get('runs')}, last exit "
                         f"{lc.get('last_exit_text')}, schedule {', '.join(r['calendar']) or r['start_interval']}"))
        if args.json:
            print(json.dumps(rows, indent=1, default=str))
        return 0 if ok else 1
    plan = launchd_plan(paths, jobs, uid, uv, runner, repoint=args.repoint)
    for p in plan:
        print(f"{p['job']}: {p['state']}" + (f" ({'; '.join(p['drift'])})" if p.get("drift") else ""))
        for a in p["actions"]:
            print(f"   {a}")
    if not args.apply:
        if any(p["actions"] for p in plan):
            print("Dry run: nothing changed. Re-run with --apply to perform the actions above.")
        return 0
    results = apply_plan(plan, runner)
    for r in results:
        print(f"{r['job']}: {r['action']} -> exit {r['exit']} {r['output']}")
    return 0 if all(r["exit"] == 0 for r in results) else 1


# ----------------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--root", help=argparse.SUPPRESS)        # another fantasy-intel checkout (audits)
        p.add_argument("--season", type=int, help="default: the synced league's season")
        p.add_argument("--db", help="live DuckDB (default FANTASY_DB_PATH); opened read-only")
        p.add_argument("--now", help="ISO timestamp to evaluate at (tests and audits)")
        p.add_argument("--from-week", type=int, help="first week to audit (default: first prospective week)")
        p.add_argument("--hosted", action="store_true", help="read MotherDuck's schema_meta (needs MOTHERDUCK_TOKEN)")
        p.add_argument("--database", default="fantasy_intel", help="MotherDuck database when MOTHERDUCK_DATABASE is unset")
        p.add_argument("--notify", action="store_true", help="macOS notification for new warn/fail issues (once each)")

    st = sub.add_parser("status", help="evidence health report (default)")
    common(st)
    st.add_argument("--json", action="store_true")
    g = sub.add_parser("guard", help="schedule-aware pregame capture check")
    common(g)
    g.add_argument("--execute", action="store_true", help="run refresh_all.py when a capture is due")
    g.add_argument("--lead-hours", type=float, default=eh.GuardConfig.lead_h)
    g.add_argument("--max-age-hours", type=float, default=eh.GuardConfig.max_age_h)
    g.add_argument("--max-attempts", type=int, default=eh.GuardConfig.max_attempts)
    g.add_argument("--retry-minutes", type=float, default=eh.GuardConfig.retry_min)
    g.add_argument("--catch-up", action="store_true",
                   help="also run a scheduled refresh the Mac missed (asleep or shut down), on the next check")
    g.add_argument("--catch-up-settle-seconds", type=float, default=90.0,
                   help="before a catch-up run, wait this long and look again for a refresh launchd started on wake")
    ld = sub.add_parser("launchd", help="verify or (idempotently) install the launchd jobs")
    ld.add_argument("action", choices=["verify", "install"])
    ld.add_argument("--jobs", default=",".join(eh.REQUIRED_JOBS), help=f"comma list of {', '.join(eh.JOBS)}")
    ld.add_argument("--apply", action="store_true", help="install: perform the plan (default prints it)")
    ld.add_argument("--repoint", action="store_true",
                    help="install: also replace jobs installed for another checkout (default: leave them alone)")
    ld.add_argument("--json", action="store_true")
    ld.add_argument("--db", help=argparse.SUPPRESS)
    ld.add_argument("--root", help=argparse.SUPPRESS)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("status", "guard", "launchd", "-h", "--help"):
        argv = ["status", *argv]
    args = ap.parse_args(argv)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "guard":
        return cmd_guard(args)
    return cmd_launchd(args)


if __name__ == "__main__":
    sys.exit(main())

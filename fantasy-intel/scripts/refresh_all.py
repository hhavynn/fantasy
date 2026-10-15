"""Full data refresh: ESPN -> nflverse -> analytics -> MotherDuck (only when MOTHERDUCK_TOKEN is
set), then a check that this week's ESPN projections were snapshotted before the first kickoff.
launchd runs it on a weekly schedule (see README "Scheduled refresh"); it is also fine by hand:

    cd backend && uv run python ../scripts/refresh_all.py [--no-publish] [--check-only]

Each step runs in its own process and the first failure stops the chain (exit 1). Every run writes
data/logs/refresh-<UTC stamp>.log with each step's output.
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

from app.config import DATA_DIR, get_settings

SCRIPTS = Path(__file__).resolve().parent
BACKEND = _bootstrap.BACKEND
LOG_DIR = DATA_DIR / "logs"
STEPS = ("sync_espn.py", "sync_nfl.py", "compute_analytics.py")
PUBLISH = "publish_motherduck.py"


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


def run_step(cmd: list[str], log: Log) -> int:
    started = time.monotonic()
    log(f"==> {Path(cmd[1]).name}  ({datetime.now().astimezone():%Y-%m-%d %H:%M %Z})")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen(cmd, cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True) as proc:
        for line in proc.stdout:
            log(line.rstrip("\n"))
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-publish", action="store_true", help="skip MotherDuck even if MOTHERDUCK_TOKEN is set")
    parser.add_argument("--check-only", action="store_true", help="only run the pregame snapshot check")
    args = parser.parse_args()

    log = Log(LOG_DIR / f"refresh-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log")
    try:
        if not args.check_only:
            steps = plan(publish=not args.no_publish)
            if PUBLISH not in steps:
                log("MotherDuck publish skipped (" + ("--no-publish" if args.no_publish else "MOTHERDUCK_TOKEN not set") + ")")
            for step in steps:
                code = run_step([sys.executable, str(SCRIPTS / step)], log)
                if code != 0:
                    log(f"FAILED: {step} exited {code}; later steps were not run. Log: {log.path}")
                    return 1
        try:
            check_pregame(log)
        except Exception as e:  # the data is refreshed; a broken check must still be visible
            log(f"WARNING: pregame snapshot check failed: {e!r}")
        log(f"Refresh complete. Log: {log.path}")
        return 0
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())

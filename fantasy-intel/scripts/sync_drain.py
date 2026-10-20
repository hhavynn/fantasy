"""Drain the multi-league sync queue (run by .github/workflows/sync-drain.yml; spec §3).

    cd backend && uv run python ../scripts/sync_drain.py [--workers 2] [--budget-min 18] [--no-maintenance]

Environment: FI_SYNC_DB_URL (fi_sync), SNAPSHOT_BUCKET + R2_* (a read/write token) or LEAGUE_FILES_DIR (local),
CREDENTIALS_PRIVKEY_V<n> (private leagues). Runs the maintenance step once (retention purge, due refreshes, probes,
recomputes), then N worker processes claim jobs until the queue is empty or the budget is used.

The log is public: library logging is switched off and every line goes through app/sync/redact.Printer (ids,
durations, counts and error codes only). Job failures are recorded in Postgres, not raised: the exit code is
non-zero only when the drain itself could not run.
"""

import argparse
import logging
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401


def objects_from_env():
    from app.config import get_settings
    from app.db.league_publish import DirWriter, R2Writer
    settings = get_settings()
    if settings.r2_configured:
        return R2Writer(settings.r2_config())
    files_dir = os.environ.get("LEAGUE_FILES_DIR", "").strip()
    if not files_dir:
        raise SystemExit("configure R2 (SNAPSHOT_BUCKET, R2_*) or LEAGUE_FILES_DIR")
    return DirWriter(Path(files_dir))


def make_context(n: int):
    from app.sync import espn_source
    from app.sync.redact import Printer
    from app.sync.worker import Context, private_keyring_from_env
    run = os.environ.get("GITHUB_RUN_ID", "local")
    return Context(sync_dsn=os.environ["FI_SYNC_DB_URL"], objects=objects_from_env(), printer=Printer(),
                   owner=f"{run}-w{n}", keyring=private_keyring_from_env(), client=espn_source.espn_client,
                   control=espn_source.control_request, probe=espn_source.probe,
                   work_root=Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()))


def worker(n: int, budget_s: float) -> int:
    logging.disable(logging.CRITICAL)
    from app.sync.redact import Printer
    from app.sync.worker import drain
    try:
        done = drain(make_context(n), budget_s=budget_s)
    except Exception as err:   # noqa: BLE001 - the type only: messages may carry connection strings or ids
        Printer()(f"worker {n} stopped: {type(err).__name__}")
        return 1
    Printer()(f"worker {n} ran {len(done)} job(s)")
    return 0


def maintenance() -> int:
    from app.sync import maintenance as m
    from app.sync.redact import Printer
    from app.sync.worker import connect
    out = Printer()
    try:
        with connect(os.environ["FI_SYNC_DB_URL"]) as con:
            purged = m.purge_expired(con, objects_from_env())
            counts = m.enqueue_due(con)
    except Exception as err:   # noqa: BLE001
        out(f"maintenance failed: {type(err).__name__}")
        return 1
    out(f"maintenance: purged {len(purged)} league(s); queued {counts['scheduled']} scheduled, {counts['probe']} probe, "
        f"{counts['recompute']} recompute")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--budget-min", type=float, default=18)
    ap.add_argument("--no-maintenance", action="store_true")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    if not os.environ.get("FI_SYNC_DB_URL", "").strip():
        print("sync drain not configured (FI_SYNC_DB_URL unset): nothing to do")
        return 0
    failed = 0 if args.no_maintenance else maintenance()
    budget_s = args.budget_min * 60
    if args.workers <= 1:
        return failed or worker(1, budget_s)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(args.workers) as pool:
        codes = pool.starmap(worker, [(n, budget_s) for n in range(1, args.workers + 1)])
    return failed or max(codes)


if __name__ == "__main__":
    sys.exit(main())

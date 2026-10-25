"""Sample free-tier usage and alert on it and on dead sync jobs (run by .github/workflows/platform-monitor.yml).

    cd backend && uv run python ../scripts/platform_monitor.py [--dry-run] [--inject metric=used/quota ...]

Environment: FI_SYNC_DB_URL (fi_sync; required), SNAPSHOT_BUCKET + R2_* (storage listing), optional
CLOUDFLARE_ANALYTICS_TOKEN (R2 operations, with R2_ACCOUNT_ID), optional VERCEL_USAGE_TOKEN (+ VERCEL_TEAM_ID),
GH_TOKEN for `gh` (the workflow's own token, issues: write).

Each run: collect every source, store the samples in fi.usage_samples, prune samples older than 90 days, list the
dead jobs of the last 7 days, then open, update or close one GitHub issue per alert kind (not with --dry-run).
`--inject metric=used/quota` adds a forced sample (alerting and printed, never stored) to prove an alert fires.

The log and the issues are public: library logging is off and only metric names, numbers, job ids, league ids and
error codes are printed. The exit code is non-zero only when the monitor itself could not run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import _bootstrap  # noqa: F401

PRUNE_SQL = "DELETE FROM fi.usage_samples WHERE sampled_at < now() - interval '90 days'"
INSERT_SQL = "INSERT INTO fi.usage_samples (metric, used, quota, unit) VALUES (%s, %s, %s, %s)"
DEAD_SQL = "SELECT job_id, league_id, kind, error_code, finished_at FROM fi.dead_jobs(interval '7 days')"


def parse_args(argv=None) -> argparse.Namespace:
    from app.monitor.collect import parse_inject

    def inject(value: str):
        try:
            return parse_inject(value)
        except ValueError as err:
            raise argparse.ArgumentTypeError(str(err)) from None

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="collect and store, but never touch GitHub issues")
    ap.add_argument("--inject", action="append", type=inject, default=[], metavar="METRIC=USED/QUOTA",
                    help="a forced sample (repeatable), e.g. vercel_invocations=800000/1000000")
    return ap.parse_args(argv)


def r2_lister_from_env():
    from app.config import get_settings
    from app.db import r2
    settings = get_settings()
    if not settings.r2_configured:
        return None
    cfg = settings.r2_config()
    return lambda: r2.list_objects(cfg, "")


def store(con, samples) -> None:
    with con.transaction():
        for s in samples:
            con.execute(INSERT_SQL, [s.metric, s.used, s.quota, s.unit])
        con.execute(PRUNE_SQL)


def dead_jobs(con):
    from app.monitor.alerts import dead_job_from_row
    try:
        rows = con.execute(DEAD_SQL).fetchall()
        con.commit()
    except Exception:   # noqa: BLE001
        con.rollback()
        return None
    return [dead_job_from_row(r) for r in rows]


def summary(out, now, results, samples, jobs, actions) -> None:
    from app.monitor.alerts import fmt_number, fmt_percent, is_alerting

    def p(line=""):
        out.write(line + "\n")
    p(f"platform monitor at {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    for r in results:
        p(f"source {r.source}: {r.status}" + (f" ({r.reason})" if r.reason else ""))
    p(f"{'metric':<38} {'used':>18} {'quota':>16} {'%':>7}  alert")
    for s in sorted(samples, key=lambda s: s.metric):
        quota = fmt_number(s.quota) if s.quota else "-"
        flag = "ALERT" if is_alerting(s) else ""
        p(f"{s.metric:<38} {fmt_number(s.used):>18} {quota:>16} {fmt_percent(s) or '-':>7}  {flag}"
          + (" (injected)" if s.injected else ""))
    p("dead jobs (7 days): " + ("unavailable" if jobs is None else str(len(jobs))))
    for j in jobs or []:
        p(f"dead job {j.job_id} league {j.league_id} kind {j.kind} error {j.error_code}")
    for kind, action in actions.items():
        p(f"issue {kind}: {action}")


def run(args, *, sync_con, http, gh, r2_lister, env, out, now=None) -> int:
    from app.monitor import alerts, collect, github
    now = now or datetime.now(timezone.utc)
    results = collect.collect_all(env, sync_con=sync_con, http=http, now=now, r2_lister=r2_lister)
    measured = [s for r in results for s in r.samples]
    failed = 0
    try:
        store(sync_con, measured)
    except Exception as err:   # noqa: BLE001 - the type only
        out.write(f"storing samples failed: {type(err).__name__}\n")
        failed = 1
    forced = {s.metric: s for s in args.inject}
    samples = [s for s in measured if s.metric not in forced] + list(forced.values())
    jobs = dead_jobs(sync_con)
    states = alerts.evaluate(samples, jobs, usage_known=all(r.status != "unavailable" for r in results))
    if args.dry_run:
        actions = {a.kind: ("would alert" if a.active else "clear") + " (dry run)" for a in states}
    else:
        actions = {}
        for a in states:
            try:
                actions[a.kind] = github.sync_alert(a, gh)
            except Exception as err:   # noqa: BLE001
                actions[a.kind] = f"failed ({type(err).__name__})"
                failed = 1
    summary(out, now, results, samples, jobs, actions)
    return failed


def main(argv=None, env=None) -> int:
    args = parse_args(argv)
    logging.disable(logging.CRITICAL)
    env = dict(os.environ if env is None else env)
    dsn = env.get("FI_SYNC_DB_URL", "").strip()
    if not dsn:
        print("platform monitor not configured (FI_SYNC_DB_URL unset): nothing to do")
        return 0
    from app.monitor.collect import urllib_http
    from app.monitor.github import gh_runner
    import psycopg
    try:
        con = psycopg.connect(dsn, prepare_threshold=None, connect_timeout=15)
    except Exception as err:   # noqa: BLE001
        print(f"platform monitor could not connect: {type(err).__name__}")
        return 1
    with con:
        return run(args, sync_con=con, http=urllib_http, gh=gh_runner, r2_lister=r2_lister_from_env(), env=env,
                   out=sys.stdout)


if __name__ == "__main__":
    sys.exit(main())

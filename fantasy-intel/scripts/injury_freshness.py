"""Injury evidence freshness before kickoff (diagnostics; changes no model input).

    cd backend
    uv run python ../scripts/injury_freshness.py report  --season 2026 --week 7 [--db PATH] [--json]
    uv run python ../scripts/injury_freshness.py refresh --season 2026 --week 7            # dry run (default)
    uv run python ../scripts/injury_freshness.py refresh --season 2026 --week 7 --execute  # runs the EXISTING syncs

`report` opens the database read-only. `refresh` prints which teams need a retrieval before their
kickoff and why; with --execute it only calls the existing nflverse / ESPN syncs (no new feed, no paid
source), which record their own retrievals. Teams already kicked off are never refreshable, and a
retrieval finishing at or after kickoff is never labeled pregame. Exit codes: report 0; refresh dry
run 0; refresh --execute 0 done, 2 nothing to refresh.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401
import duckdb

from app.analytics import injury_freshness as inj
from app.config import get_settings


def _now(args) -> datetime:
    if not args.now:
        return datetime.now(timezone.utc)
    t = datetime.fromisoformat(args.now)
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)   # naive = UTC


def _thresholds(args) -> inj.Thresholds:
    return inj.Thresholds(stale_after_hours=args.stale_hours, window_hours=args.window_hours)


def _open_ro(path: Path):
    if not Path(path).exists():
        raise SystemExit(f"database {path} not found")
    return duckdb.connect(str(path), read_only=True)


def _fmt_h(h) -> str:
    return "-" if h is None else f"{h:.1f}h"


def render_report(rep: dict) -> str:
    s = rep["summary"]
    lines = [f"Injury evidence freshness  season {rep['season']} week {rep['week']}  as of {rep['now']}",
             f"thresholds: stale if retrieved > {rep['thresholds']['stale_after_hours']:g}h before reference and "
             f"kickoff within {rep['thresholds']['window_hours']:g}h (or unknown)"]
    if not rep["log_present"]:
        lines.append("WARNING: no injury_retrievals table; no retrieval has ever been verified in this database.")
    lines.append(f"teams {s['teams']}: " + ", ".join(f"{k} {v}" for k, v in s["by_status"].items())
                 + " | source: " + ", ".join(f"{k} {v}" for k, v in s["by_source"].items()))
    pt = s["publication_time"]
    lines.append(f"provider publication time: {pt['with_provider_time']} of {pt['used_evidence']} used evidence rows "
                 f"carry one; {pt['null_provider_time']} NULL (never imputed)")
    if s["espn_fallback_without_designations"]:
        lines.append("ESPN fallback adds no designation for: " + ", ".join(s["espn_fallback_without_designations"])
                     + " (indistinguishable from healthy to the model)")
    if s["unknown_kickoff"]:
        lines.append("unknown kickoff: " + ", ".join(s["unknown_kickoff"]))
    lines += ["", f"{'team':5} {'kickoff (UTC)':17} {'to KO':>8} {'source':12} {'status':17} {'age':>8}  reason"]
    for t in rep["teams"]:
        ko = (t["kickoff"] or "unknown")[:16].replace("T", " ")
        lines.append(f"{t['team']:5} {ko:17} {_fmt_h(t['hours_to_kickoff']):>8} {t['source_used']:12} "
                     f"{t['status']:17} {_fmt_h(t['hours_since_retrieval']):>8}  {t['reason']}")
    if rep["upcoming_issues"]:
        lines += ["", "Upcoming games with missing or outdated injury evidence:"]
        lines += [f"  {i['team']} vs {i['opponent']}  kickoff {(i['kickoff'] or 'unknown')[:16]}  "
                  f"{i['status']}: {i['reason']}" for i in rep["upcoming_issues"]]
    return "\n".join(lines)


def render_plan(plan: dict) -> str:
    lines = [f"Refresh plan  season {plan['season']} week {plan['week']}  as of {plan['now']}"]
    if not plan["refreshable"]:
        lines.append("nothing to refresh before kickoff")
    for r in plan["refreshable"]:
        lines.append(f"  {r['team']:4} refresh {'+'.join(r['sources'])} before {(r['kickoff'] or 'unknown kickoff')[:16]}"
                     f"  [{r['status']}: {r['reason']}]" + (f"  NOTE {r['caveat']}" if r["caveat"] else ""))
    for b in plan["not_refreshable"]:
        lines.append(f"  {b['team']:4} NOT refreshable: {b['reason']}")
    if plan["up_to_date"]:
        lines.append("up to date: " + ", ".join(plan["up_to_date"]))
    lines.append(plan["uses"])
    return "\n".join(lines)


def cmd_report(args) -> int:
    con = _open_ro(args.db)
    try:
        rep = inj.diagnostics(con, args.season, args.week, _now(args), thresholds=_thresholds(args))
    finally:
        con.close()
    print(json.dumps(rep, indent=1, default=str) if args.json else render_report(rep))
    return 0


def cmd_refresh(args) -> int:
    con = _open_ro(args.db)
    try:
        now = _now(args)
        plan = inj.plan_refresh(con, args.season, args.week, now, kickoffs=inj.scheduled_kickoffs(
            con, args.season, args.week), thresholds=_thresholds(args))
    finally:
        con.close()
    print(json.dumps(plan, indent=1, default=str) if args.json else render_plan(plan))
    if not args.execute:
        print("\n(dry run: pass --execute to run the existing syncs)")
        return 0
    from app.db.connection import open_db
    from app.espn.sync import run_espn_sync
    from app.nfl.sync import run_nfl_sync

    settings = get_settings()
    try:
        out = inj.execute_refresh(
            plan,
            sync_nfl=lambda: run_nfl_sync(lambda: open_db(args.db), args.season),
            sync_espn=lambda: run_espn_sync(lambda: open_db(args.db), settings))
    except inj.RefreshRefused as err:
        print(f"refused: {err}")
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0


def main(argv=None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("report", cmd_report), ("refresh", cmd_refresh)):
        p = sub.add_parser(name)
        p.add_argument("--season", type=int, default=settings.nfl_season)
        p.add_argument("--week", type=int, required=True)
        p.add_argument("--db", type=Path, default=settings.db_path)
        p.add_argument("--json", action="store_true")
        p.add_argument("--now", help="ISO time to evaluate at (default: the current time)")
        p.add_argument("--stale-hours", type=float, default=inj.DEFAULT_THRESHOLDS.stale_after_hours)
        p.add_argument("--window-hours", type=float, default=inj.DEFAULT_THRESHOLDS.window_hours)
        if name == "refresh":
            p.add_argument("--dry-run", action="store_true", help="print the plan only (the default)")
            p.add_argument("--execute", action="store_true", help="run the existing syncs for the planned sources")
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    if getattr(args, "dry_run", False) and getattr(args, "execute", False):
        ap.error("--dry-run and --execute are mutually exclusive")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

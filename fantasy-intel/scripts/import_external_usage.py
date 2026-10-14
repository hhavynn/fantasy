"""Import a normalized external utilization CSV (routes / situational usage).

    cd backend && uv run python ../scripts/import_external_usage.py path/to/file.csv \
        --provider test_provider --dataset weekly_routes

Generic, provider-neutral format: see docs/external_utilization_contract.md.
Keep real provider files under data/imports/external/ (git-ignored).
"""

import argparse
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from app.analytics.pipeline import run_analytics
from app.config import get_settings
from app.db.connection import fetch_dicts, open_db
from app.external.importer import import_external_usage


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path)
    ap.add_argument("--provider", help="provider name if the file has no provider column")
    ap.add_argument("--dataset", help="dataset name if the file has no dataset column")
    ap.add_argument("--no-analytics", action="store_true", help="skip recomputing player_week_usage/analytics")
    args = ap.parse_args()
    if not args.csv.exists():
        print(f"file not found: {args.csv}", file=sys.stderr)
        return 2
    settings = get_settings()
    with open_db(settings.db_path) as con:
        rec = import_external_usage(con, args.csv, provider=args.provider, dataset=args.dataset)
        print(json.dumps({k: rec[k] for k in ("import_id", "status", "provider", "dataset", "season", "weeks",
                                               "row_count", "matched_rows", "unmatched_rows", "ambiguous_rows",
                                               "warning_count", "error_count", "message")}, indent=2, default=str))
        for i in fetch_dicts(con, """SELECT severity, check_name, detail, row_number, player_name FROM external_usage_issues
                                     WHERE import_id = ? ORDER BY severity, row_number LIMIT 40""", [rec["import_id"]]):
            print(f"  {i['severity'].upper():7} row {i['row_number'] or '-':>4} {i['check_name']}: {i['detail']}"
                  + (f" [{i['player_name']}]" if i["player_name"] else ""))
        if rec["status"] == "imported" and not args.no_analytics:
            for season in sorted({rec["season"]} if rec["season"] else {settings.nfl_season}):
                stats = run_analytics(con, season)
                print("analytics:", json.dumps(stats.get("usage", {}).get("external_provider"), default=str),
                      "rows_with_routes:", stats.get("usage", {}).get("rows_with_routes"))
    return 1 if rec["status"] == "rejected" else 0


if __name__ == "__main__":
    sys.exit(main())

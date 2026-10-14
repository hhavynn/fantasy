"""Print aggregate ESPN calibration errors; never sync, migrate or write the database.

    cd backend && uv run python ../scripts/backtest_espn_projections.py --season 2026
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

import _bootstrap  # noqa: F401
import duckdb

from app.analytics.projection_backtest import backtest
from app.config import get_settings


def format_report(report: dict) -> str:
    lines = ["ESPN Projection Accuracy & Calibration Lab", report["overall"]["status"],
             f"Eligibility cutoff: {report['as_of']}; sample floor: {report['min_samples']}",
             "Signed bias = actual - projected; positive means ESPN under-projected.",
             "Group                             n      MAE     RMSE     Bias  Sample status"]

    def row(label, metrics):
        def number(value):
            return f"{value:.3f}" if value is not None else "—"
        lines.append(f"{label:30} {metrics['n']:5d} {number(metrics['mae']):>8} "
                     f"{number(metrics['rmse']):>8} {number(metrics['bias']):>8}  {metrics['status']}")

    row("Overall (latest pregame)", report["overall"])
    for name, metrics in report["by_position"].items():
        row(name, metrics)
    threshold = report["high_projection_threshold"]
    for position, groups in report["by_position_projected_points"].items():
        for name, metrics in groups.items():
            row(f"{position}: projected {'>=' if name == 'higher' else '<'} {threshold:g}", metrics)
    for horizon, metrics in report["by_horizon"].items():
        row(f"Horizon {horizon}", metrics)
    lines.extend(["", "Paired horizons (same player-weeks; far MAE minus near MAE):"])
    for pair in report["paired_horizon_comparisons"]:
        delta = pair["far_minus_near_mae"]
        value = f"{delta:.3f}" if delta is not None else "—"
        lines.append(f"  {pair['near']} vs {pair['far']}: n={pair['n']}, "
                     f"delta={value}; {pair['near_metrics']['status']}")
    lines.extend(["", "Audit counts:"])
    lines.extend(f"  {name}: {value}" for name, value in report["counts"].items())
    lines.append("Actual provenance (matching closed-week raw ESPN captures):")
    lines.extend(f"  {name}: {value}" for name, value in report["actual_provenance"].items())
    lines.append("Exclusions (one primary reason per candidate player-week):")
    lines.extend(f"  {name}: {value}" for name, value in report["exclusions"].items())
    lines.extend(["", report["cohort_note"], *report["schema_issues"], *report["warnings"]])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Existing DuckDB file (defaults to app settings)")
    parser.add_argument("--season", type=int, help="Limit observations to this season (recommended)")
    parser.add_argument("--raw-archive-root", type=Path, help="Existing ESPN raw archive directory (default: DB parent/raw/espn)")
    parser.add_argument("--as-of", type=datetime.fromisoformat, help="Timezone-qualified eligibility cutoff; not a historical replay")
    parser.add_argument("--min-samples", type=int, default=30, help="Descriptive reporting floor, not statistical certainty")
    parser.add_argument("--high-projection-threshold", type=float, default=10.0, help="Scoring-volume proxy cutoff in fantasy points")
    parser.add_argument("--json", action="store_true", help="Print aggregate JSON only (no player records)")
    args = parser.parse_args(argv)
    path = args.db if args.db is not None else get_settings().db_path
    con = None
    try:
        if path.exists():
            con = duckdb.connect(str(path), read_only=True)
        result = backtest(con, season=args.season, as_of=args.as_of,
                          min_samples=args.min_samples, high_projection_threshold=args.high_projection_threshold,
                          raw_archive_root=args.raw_archive_root if args.raw_archive_root is not None else path.parent / "raw/espn")
    except (ValueError, duckdb.Error) as err:
        # Do not print DB exceptions: they can include a row's private values.
        print(str(err) if isinstance(err, ValueError) else "Unable to read the existing database (it may be busy or invalid).", file=sys.stderr)
        return 2
    finally:
        if con is not None:
            con.close()
    print(json.dumps(result, indent=2, allow_nan=False) if args.json else format_report(result))
    return 0  # An insufficient report is a successful measurement, not a setup failure.


if __name__ == "__main__":
    sys.exit(main())

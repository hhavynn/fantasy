"""Prospective ESPN benchmark of the production independent model (read-only; see docs/espn_benchmark.md).

    cd backend
    uv run python ../scripts/espn_benchmark.py score  --season 2026 [--weeks 1-6] [--json]
    uv run python ../scripts/espn_benchmark.py record --season 2026 --week 6

`score` opens the database read-only and prints the scorecard (a table, or the full JSON with
--json). `record` scores weeks 1..W and writes the scorecard once to --out
(default data/models/espn_benchmark, git-ignored) as s<season>w<week>-<content sha>.json: re-running
with identical inputs writes nothing; changed inputs (a newly final game, a new run) write a NEW
file; an existing file is never overwritten. Neither command writes the database, trains, syncs or
changes any projection.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import _bootstrap  # noqa: F401
import duckdb

from app.analytics import espn_benchmark as eb
from app.config import REPO_ROOT, get_settings


def _weeks(text: str | None) -> list[int] | None:
    if not text:
        return None
    out: set[int] = set()
    for part in text.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return sorted(out)


def _open(path: Path):
    if not Path(path).exists():
        raise FileNotFoundError(f"database {path} not found")
    return duckdb.connect(str(path), read_only=True)


def _config(args) -> eb.BenchmarkConfig:
    return eb.BenchmarkConfig(resamples=args.resamples, seed=args.seed, min_weeks=args.min_weeks,
                              horizon_window_h=args.horizon_window)


def _table(card: dict) -> str:
    if not card["available"]:
        return f"Benchmark unavailable: {card['unavailable_reason']}"
    lines = [f"ESPN benchmark, season {card['season']}, weeks {card['weeks']}, model {card['model_version']}"]
    need = card["params"]["espn_min_matched_per_position"]
    lines.append(f"evidence gate: >= {need} model-covered rows and >= {card['params']['min_weeks']} weeks; "
                 "MAE diff = ours - ESPN (negative: ours closer)")
    for name, c in card["comparisons"].items():
        extra = f", fallback share {c['fallback_share']}" if "fallback_share" in c else ""
        lines.append(f"\n{name}: {c['verdict']} ({c['verdict_reason']}){extra}")
        lines.append(f"  {'pos':4} {'n':>6} {'weeks':>5} {'MAE ours':>9} {'MAE espn':>9} {'diff':>7}  95% CI")
        for pos, g in c["by_position"].items():
            lines.append(f"  {pos:4} {g['n']:>6} {g['weeks_covered']:>5} {g['ours']['mae']!s:>9} "
                         f"{g['espn']['mae']!s:>9} {g['mae_diff']!s:>7}  {g['ci95']}  [{g['verdict']}]")
    cov = card["coverage"]
    lines.append(f"\nrows {cov['rows']}, scored {cov['scored']}")
    lines.append("exclusions: " + json.dumps(cov["by_exclusion_reason"]))
    lines.append("outcomes:   " + json.dumps(cov["by_outcome_status"]))
    if card["reader_exclusions"]:
        lines.append(f"{len(card['reader_exclusions'])} forecast run/row exclusions from the reader (see --json)")
    return "\n".join(lines)


def cmd_score(args) -> int:
    con = _open(args.db)
    try:
        card = eb.scorecard(con, args.season, _weeks(args.weeks), config=_config(args))
    finally:
        con.close()
    print(eb.to_json(card) if args.json else _table(card), end="" if args.json else "\n")
    return 0


def cmd_record(args) -> int:
    con = _open(args.db)
    try:
        card = eb.scorecard(con, args.season, list(range(1, args.week + 1)), config=_config(args))
    finally:
        con.close()
    if not card["available"]:
        print(f"NOT READY: {card['unavailable_reason']}", file=sys.stderr)
        return 3
    path, created = eb.record(card, args.out, args.season, args.week)
    print(json.dumps({"path": str(path), "created": created, "sha256": eb.content_sha(eb.to_json(card))}, indent=1))
    return 0


def main(argv=None) -> int:
    settings = get_settings()
    out_default = Path(os.getenv("ESPN_BENCHMARK_DIR") or "data/models/espn_benchmark")
    out_default = out_default if out_default.is_absolute() else REPO_ROOT / out_default
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("score", "record"):
        p = sub.add_parser(name)
        p.add_argument("--season", type=int, default=settings.nfl_season)
        p.add_argument("--db", type=Path, default=settings.db_path, help="DuckDB file, opened read-only")
        p.add_argument("--resamples", type=int, default=eb.BenchmarkConfig.resamples)
        p.add_argument("--seed", type=int, default=eb.SEED)
        p.add_argument("--min-weeks", type=int, default=eb.BenchmarkConfig.min_weeks)
        p.add_argument("--horizon-window", type=float, default=eb.BenchmarkConfig.horizon_window_h,
                       help="hours: max |ours horizon - ESPN horizon| for the horizon-matched comparison")
        if name == "score":
            p.add_argument("--weeks", help="e.g. 1-6 or 2,4,5 (default: every week with ESPN history)")
            p.add_argument("--json", action="store_true")
        else:
            p.add_argument("--week", type=int, required=True, help="score weeks 1..WEEK")
            p.add_argument("--out", type=Path, default=out_default)
    args = ap.parse_args(argv)
    try:
        return {"score": cmd_score, "record": cmd_record}[args.command](args)
    except (FileNotFoundError, ValueError, RuntimeError) as err:
        print(f"NOT READY: {err}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())

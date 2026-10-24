"""Issue #198 step 2 (research only): does a defense's allowed pass-direction profile predict where opponents throw
against it next game, beyond the league average and the offense's own tendency?

    cd backend && uv run python ../scripts/pass_direction_research.py --out-dir /tmp/pass_direction

Downloads regular-season play-by-play for 2022-2025 from nflverse (nflreadpy), builds the six pass cells, predicts
each attempt from same-season games strictly before its week and evaluates the pre-registered rule in
app/analytics/pass_direction.py. Writes <out-dir>/{preds.parquet, report.json} (outside the repo) and prints the
report. Nothing is written to any database; no app code reads the output.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401

import nflreadpy as nfl
import polars as pl

from app.analytics import pass_direction as pdr


def git_sha() -> str | None:
    """The checked-out commit, plus "-dirty" when tracked files differ from it (pre-registration evidence)."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                               text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return sha + ("-dirty" if dirty else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    t = time.time()
    lo, hi = pdr.EVAL_SEASONS
    pbp = nfl.load_pbp(list(range(lo, hi + 1)))
    att, counts = pdr.attempts(pbp)
    preds = pdr.predict(att)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    preds.write_parquet(args.out_dir / "preds.parquet")
    cell_mix = {c: round(float((att["cell"] == c).mean()), 4) for c in pdr.CELLS}
    report = {"git_sha": git_sha(), "nflreadpy": getattr(nfl, "__version__", None), "seasons": [lo, hi],
              "weeks": list(pdr.EVAL_WEEKS), "attempt_counts": counts, "league_cell_mix": cell_mix,
              **pdr.evaluate(preds), "seconds": round(time.time() - t, 1)}
    (args.out_dir / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

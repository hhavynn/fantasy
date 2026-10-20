"""Rebuild tests/shard_weights.json (seconds per test file) from a full run's durations, for `pytest --shard`.

    uv run pytest -q -p no:cacheprovider --durations=0 --durations-min=0 > durations.txt
    uv run python scripts/shard_weights.py durations.txt

Weights only balance the CI shards; a stale or missing entry never skips a test (unknown files count as the median)."""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

LINE = re.compile(r"^\s*([0-9.]+)s (?:setup|call|teardown)\s+(tests/[^:]+\.py)::")


def main() -> int:
    totals: dict[str, float] = defaultdict(float)
    for line in Path(sys.argv[1]).read_text().splitlines():
        m = LINE.match(line)
        if m:
            totals[m[2]] += float(m[1])
    out = Path(__file__).resolve().parents[1] / "tests" / "shard_weights.json"
    out.write_text(json.dumps({f: round(s, 1) for f, s in sorted(totals.items())}, indent=0) + "\n")
    print(f"{len(totals)} files, {sum(totals.values()):.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

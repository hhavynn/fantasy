"""Fail when a sync drain log holds anything that looks like a credential or provider data (spec §3.8).

    python3 scripts/audit_drain_log.py <drain.log>

The drain prints opaque ids, durations, counts and codes only (app/sync/redact.py). This second check runs in the
workflow on the captured log: a SWID-shaped GUID, an espn_s2-like token, a cookie name with a value, an ESPN league
URL, or any line that is not one of the drain's known shapes fails the run. Standard library only.
"""

import re
import sys

ALLOWED = [
    re.compile(r"^sync drain not configured .*$"),
    re.compile(r"^maintenance: purged \d+ league\(s\); queued \d+ scheduled, \d+ probe, \d+ recompute$"),
    re.compile(r"^maintenance failed: [A-Za-z_]+$"),
    re.compile(r"^job [0-9a-f-]{36} league lg_[A-Za-z0-9_-]{16,64} kind [a-z]+ attempt \d+ -> [a-z0-9_:]+ in \d+\.\ds$"),
    re.compile(r"^worker \d+ ran \d+ job\(s\)$"),
    re.compile(r"^worker \d+ stopped: [A-Za-z_]+$"),
    re.compile(r"^\[line withheld: contained league data\]$"),
    re.compile(r"^::add-mask::.*$"),
]
FORBIDDEN = [
    re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}"),   # SWID
    re.compile(r"AE[A-Za-z0-9%+/=]{40,}"),                                                          # espn_s2-like
    re.compile(r"(espn_s2|SWID)\s*[=:]", re.I),
    re.compile(r"fantasy\.espn\.com", re.I),
]


def audit(lines: list[str]) -> list[str]:
    problems = []
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.startswith("::add-mask::"):
            continue
        if any(p.search(line) for p in FORBIDDEN):
            problems.append(f"line {n}: credential- or provider-shaped text")
        elif not any(p.match(line) for p in ALLOWED):
            problems.append(f"line {n}: not a known drain line")
    return problems


def main() -> int:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        problems = audit(f.readlines())
    for p in problems:
        print(p)                       # line numbers only: never echo the offending text into the public log
    print(f"drain log audit: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

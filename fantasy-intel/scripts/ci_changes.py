#!/usr/bin/env python3
"""Classify a CI run as docs-only or full (the `changes` job in .github/workflows/ci.yml).

A pull request is docs-only when every path its diff touches (merge base of the base branch .. PR head, renames
counted as a deletion plus an addition) is on DOCS_ONLY and not on NOT_DOCS_ONLY. Everything else gets full CI:
any other path, an empty or undeterminable diff, git errors, and every event that is not a pull request (a push to
main, workflow_dispatch). Fail closed: when in doubt, run everything.

    python3 fantasy-intel/scripts/ci_changes.py --event pull_request --base <base sha> --head <head sha>

Prints `docs_only=true|false` and `reason=...` (append it to $GITHUB_OUTPUT). Stdlib only; never exits non-zero
for a classification problem (that is a full run), only for bad arguments.
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys

# Markdown that nothing builds, tests or reads. Patterns use fnmatch on the repository-relative path ("*" also
# crosses "/"), so "docs/*.md" is every Markdown file anywhere under the root docs/ tree.
DOCS_ONLY: tuple[str, ...] = (
    "docs/*.md",
    "README.md",
)

# Markdown that tests or code read, so a change to it can turn CI red. Keep it in step with the scan in
# backend/tests/test_ci_changes.py (which fails when a test or script starts reading a root doc not listed here).
NOT_DOCS_ONLY: tuple[str, ...] = (
    "docs/agent-workflow.md",                          # tests/test_agent_check.py (404 contract)
    "docs/agent-security-review.md",                   # tests/test_agent_check.py (404 contract)
    "docs/multileague/provider-capability-matrix.md",  # tests/test_league_providers.py (capability rows)
)
# Deliberately not docs-only: AGENTS.md, CLAUDE.md, GEMINI.md and .claude/** (agent policy that the verification
# tooling and its tests describe), .github/** (workflows, PR template), and fantasy-intel/docs/** (research docs whose
# pre-registration blocks are hashed or existence-checked by scripts and tests: rb_research, shadow_promotion_check,
# situational_usage_research, ...).


def is_docs_path(path: str) -> bool:
    if not path or path != path.strip() or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return False                                   # odd names (control characters, padding) are never docs
    if path.startswith("/") or ".." in path.split("/"):
        return False
    if any(fnmatch.fnmatchcase(path, p) for p in NOT_DOCS_ONLY):
        return False
    return any(fnmatch.fnmatchcase(path, p) for p in DOCS_ONLY)


def classify(paths: list[str]) -> tuple[bool, str]:
    """(docs_only, reason) for the paths a diff touches."""
    if not paths:
        return False, "empty diff: full CI"
    others = [p for p in paths if not is_docs_path(p)]
    if others:
        more = f" (+{len(others) - 1} more)" if len(others) > 1 else ""
        return False, f"non-docs path {others[0]}{more}: full CI"
    return True, f"docs-only change ({len(paths)} Markdown path(s)): backend/frontend/E2E not applicable"


def changed_paths(base: str, head: str) -> list[str]:
    """Paths touched between the merge base of `base` and `head`, and `head`. Renames count as both paths."""
    merge_base = subprocess.run(["git", "merge-base", base, head], capture_output=True, text=True, check=True)
    diff = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", merge_base.stdout.strip(), head],
                          capture_output=True, text=True, check=True)
    return [p for p in diff.stdout.split("\0") if p]


def decide(event: str, base: str, head: str) -> tuple[bool, str]:
    if event != "pull_request":
        return False, f"{event or 'unknown'} event: full CI"
    if not base or not head:
        return False, "missing base or head SHA: full CI"
    try:
        paths = changed_paths(base, head)
    except (OSError, subprocess.CalledProcessError) as exc:
        return False, f"diff undeterminable ({type(exc).__name__}): full CI"
    return classify(paths)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--event", required=True)
    ap.add_argument("--base", default="")
    ap.add_argument("--head", default="")
    args = ap.parse_args(argv)
    docs_only, reason = decide(args.event, args.base, args.head)
    print(f"docs_only={'true' if docs_only else 'false'}")
    print("reason=" + "".join(c if c.isprintable() else "?" for c in reason))   # one $GITHUB_OUTPUT line
    return 0


if __name__ == "__main__":
    sys.exit(main())

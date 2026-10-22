"""Alert issues on the public repository through the `gh` CLI (plan Decisions, "Alerts").

One open issue per alert kind, found by its exact title among the open `platform-alert` issues. The issue body ends
with a hidden `<!-- state: k1,k2 -->` marker naming what alerted at the last run:

  * active, no open issue        -> create it (GitHub emails the owner)                        "opened"
  * active, a key not in marker  -> comment with the new keys (another email), update the body  "commented"
  * active, nothing new          -> update the body (the numbers) silently                      "unchanged"
  * inactive, open issue         -> close it with a "Cleared" comment                           "closed"
  * inactive and the input could not be read                                                  "unknown" (left open)
  * inactive, no open issue                                                                   "none"

`gh` runs through an injectable runner (argv without the leading "gh" -> stdout) so tests use a fake. Errors carry
the exit status only: gh's stderr could echo a body.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

from .alerts import AlertState

LABEL = "platform-alert"
MAX_MARKER_KEYS = 1000
_MARKER = re.compile(r"<!-- state: ([A-Za-z0-9_,-]*) -->")
Runner = Callable[[list[str]], str]


class GhError(RuntimeError):
    def __init__(self, returncode: int):
        super().__init__(f"gh exited with status {returncode}")
        self.returncode = returncode


def gh_runner(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise GhError(proc.returncode)
    return proc.stdout


def marker(keys) -> str:
    return f"<!-- state: {','.join(sorted(keys)[:MAX_MARKER_KEYS])} -->"


def parse_marker(body: str | None) -> set[str] | None:
    m = _MARKER.search(body or "")
    if m is None:
        return None
    return {k for k in m.group(1).split(",") if k}


def render_body(alert: AlertState) -> str:
    return (f"{alert.body}\n\nOpened and updated by the hourly platform monitor "
            f"(`.github/workflows/platform-monitor.yml`). It closes itself when the condition clears.\n\n"
            f"{marker(alert.keys)}\n")


def find_open_issue(run: Runner, title: str) -> dict | None:
    out = run(["issue", "list", "--label", LABEL, "--state", "open", "--limit", "100", "--json", "number,title,body"])
    matches = [i for i in json.loads(out or "[]") if i.get("title") == title]
    return min(matches, key=lambda i: i["number"]) if matches else None


def sync_alert(alert: AlertState, run: Runner) -> str:
    issue = find_open_issue(run, alert.title)
    if not alert.active:
        if issue is None:
            return "none"
        if not alert.known:
            return "unknown"
        run(["issue", "close", str(issue["number"]), "--comment",
             f"Cleared: {alert.body} Closing this issue."])
        return "closed"
    body = render_body(alert)
    if issue is None:
        run(["issue", "create", "--title", alert.title, "--label", LABEL, "--body", body])
        return "opened"
    previous = parse_marker(issue.get("body")) or set()
    new = [k for k in alert.keys if k not in previous]
    run(["issue", "edit", str(issue["number"]), "--body", body])
    if not new:
        return "unchanged"
    lines = "\n".join(f"- {alert.details.get(k, f'`{k}`')}" for k in new)
    run(["issue", "comment", str(issue["number"]), "--body", f"New since the last report:\n\n{lines}"])
    return "commented"


def sync_all(alerts: list[AlertState], run: Runner) -> dict[str, str]:
    return {a.kind: sync_alert(a, run) for a in alerts}

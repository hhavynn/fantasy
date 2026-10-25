"""The drain's only output path (spec §3.8: GitHub Actions logs are public).

Workers print opaque ids (`lg_…`, job uuids), durations, counts and error codes, nothing else. As a second line of
defence, every string the run learns is sensitive (team, member and player names, the provider's league id, cookies)
is registered here, and a line containing one is replaced before it is printed. Cookies are also `::add-mask::`ed
in Actions, so GitHub itself hides them if anything slips past.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterable, TextIO

REDACTED = "[line withheld: contained league data]"
NAME_KEYS = frozenset({"name", "nickname", "location", "abbrev", "fullName", "firstName", "lastName", "displayName",
                       "teamName"})
MIN_LEN = 3            # shorter strings (abbreviations like "NY") would withhold ordinary lines


class Printer:
    def __init__(self, stream: TextIO | None = None, *, github: bool | None = None):
        self.stream = stream or sys.stdout
        self.github = bool(os.environ.get("GITHUB_ACTIONS")) if github is None else github
        self._sensitive: set[str] = set()

    def mask(self, value: str | None) -> None:
        """A secret (cookie): masked by GitHub and withheld here."""
        if value:
            if self.github:
                self.stream.write(f"::add-mask::{value}\n")
            self._sensitive.add(value)

    def sensitive(self, values: Iterable[Any]) -> None:
        for v in values:
            if isinstance(v, (str, int)) and len(str(v)) >= MIN_LEN:
                self._sensitive.add(str(v))

    def sensitive_from(self, payload: Any) -> None:
        """Register every name-like string in a provider payload (walks dicts and lists)."""
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in NAME_KEYS and isinstance(v, str):
                        self.sensitive([v])
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)

    def clean(self, line: str) -> str:
        return REDACTED if any(s in line for s in self._sensitive) else line

    def __call__(self, *parts: Any) -> None:
        self.stream.write(self.clean(" ".join(str(p) for p in parts)) + "\n")
        self.stream.flush()

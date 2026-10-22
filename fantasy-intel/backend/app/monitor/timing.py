"""Request timings without private data (issue #201): a `Server-Timing` header on every response and an opt-in,
sampled log line.

The header names phases only (`auth`, `files`, `wait`, `app`) with an optional short code (`cold`, `warm`, `hit`,
`miss`, `stale`, a size bucket); it never carries a league id, a name or a query string. The Next.js server reads it
(lib/api.ts) and browsers never see the API's headers.

The log line is off unless FI_PERF_SAMPLE is set (a fraction of requests, 0-1), so free-tier log volume stays flat. It
names the route *template* (`/v2/leagues/{league_id}/api/overview`), the status, the duration, the phases and the
league file's size bucket. FI_PERF_SLOW_MS additionally logs every request slower than that many milliseconds.
"""

from __future__ import annotations

import logging
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger("fi.perf")
_CODE = re.compile(r"[a-z0-9-]{1,16}")
_NAME = re.compile(r"[a-z]{1,16}")
# Size buckets for a league file (the version names its byte count): coarse on purpose, never an identifier.
SIZE_BUCKETS = ((1 << 20, "xs"), (8 << 20, "s"), (32 << 20, "m"), (128 << 20, "l"))


def size_bucket(nbytes: int | None) -> str:
    if not nbytes or nbytes < 0:
        return "unknown"
    for limit, name in SIZE_BUCKETS:
        if nbytes < limit:
            return name
    return "xl"


def version_bytes(version: str) -> int | None:
    """The byte count a league/global version names (`<stamp>Z-<sha>-<bytes>`), or None."""
    from ..db.league_split import VERSION_RE
    m = VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    return int(m["bytes"]) if m else None


class Timing:
    """Phases of one request. Names and codes are checked against short fixed shapes, so nothing a request supplies
    can reach the header or the log."""

    __slots__ = ("start", "phases", "size")

    def __init__(self, clock=time.perf_counter):
        self.start = clock()
        self.phases: list[tuple[str, float, str | None]] = []    # (name, milliseconds, code)
        self.size: str | None = None

    def add(self, name: str, seconds: float, code: str | None = None) -> None:
        if not _NAME.fullmatch(name):
            return
        self.phases.append((name, max(0.0, seconds) * 1000, code if code and _CODE.fullmatch(code) else None))

    @contextmanager
    def phase(self, name: str, code: str | None = None) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - t0, code)

    def set_code(self, name: str, code: str) -> None:
        """Attach a code to the last phase called `name` (known only once it has finished)."""
        for i in range(len(self.phases) - 1, -1, -1):
            n, ms, _ = self.phases[i]
            if n == name:
                self.phases[i] = (n, ms, code if _CODE.fullmatch(code) else None)
                return

    def header(self, total_s: float) -> str:
        parts = [f'{n};dur={ms:.1f}' + (f';desc="{c}"' if c else "") for n, ms, c in self.phases]
        parts.append(f"app;dur={max(0.0, total_s) * 1000:.1f}" + (f';desc="{self.size}"' if self.size else ""))
        return ", ".join(parts)


def _float_env(name: str) -> float:
    try:
        return max(0.0, float((os.environ.get(name) or "0").strip()))
    except ValueError:
        return 0.0


def _logger() -> logging.Logger:
    """Its own stderr handler: the API configures no logging, and INFO would otherwise be dropped."""
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def route_template(scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def should_log(total_ms: float, *, rand=random.random) -> bool:
    sample, slow_ms = _float_env("FI_PERF_SAMPLE"), _float_env("FI_PERF_SLOW_MS")
    return (slow_ms > 0 and total_ms >= slow_ms) or (sample > 0 and rand() < sample)


def log_line(scope, status: int, timing: Timing, total_s: float) -> str:
    phases = " ".join(f"{n}={ms:.0f}" + (f"/{c}" if c else "") for n, ms, c in timing.phases)
    return (f"perf {scope.get('method', '?')} {route_template(scope)} status={status} total_ms={total_s * 1000:.0f}"
            f" size={timing.size or '-'}" + (f" {phases}" if phases else ""))


class ServerTimingMiddleware:
    """Pure ASGI (no body buffering): a Timing on `scope["state"]["fi_timing"]` for the request's code to add phases
    to, and the `Server-Timing` header on the response start."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        timing = Timing()
        scope.setdefault("state", {})["fi_timing"] = timing
        status = 0

        async def send_with_timing(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message.get("status", 0))
                headers = [h for h in message.get("headers", []) if h[0].lower() != b"server-timing"]
                headers.append((b"server-timing", timing.header(time.perf_counter() - timing.start).encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            total = time.perf_counter() - timing.start
            if should_log(total * 1000):
                _logger().info(log_line(scope, status or 500, timing, total))


def request_timing(request) -> Timing | None:
    """The request's Timing, or None outside the middleware (unit tests that build their own app)."""
    timing = getattr(request.state, "fi_timing", None)
    return timing if isinstance(timing, Timing) else None

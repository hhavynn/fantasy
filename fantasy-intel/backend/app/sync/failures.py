"""Spec §3.6 failure classification: what a sync job does with an error. Pure: no I/O.

    retry    network, 5xx, 429 (also pauses the provider), R2 / Postgres transient, anything unknown: backoff, then dead
    auth     authenticated 401/403 on a private league: ambiguous; Postgres decides suspect vs invalid
             (fi.record_credential_failure: >= 3 over >= 6 h with a control success), the job retries meanwhile
    private  401/403 without cookies: the provider says the league is private; restrict it now (the safe direction)
    missing  404 league not found: Postgres decides when it is `unavailable` (same rule); data is kept
    dead     our parsing or validation failed: retrying cannot help; the previous data stays served

An Outcome holds a fixed error code and never the exception's message: provider messages carry league ids and the
drain's logs are public.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

_HTTP = re.compile(r"HTTP (\d{3})")


@dataclass(frozen=True)
class Outcome:
    action: str                 # retry | auth | private | missing | dead
    code: str
    pause_provider: bool = False


def _status(err: BaseException) -> int | None:
    m = _HTTP.search(str(err))
    return int(m[1]) if m else None


def classify(err: BaseException, *, authenticated: bool, stage: str = "build") -> Outcome:
    """`authenticated`: the request carried the league's cookies. `stage`: "fetch" (talking to the provider),
    "build" (our parsing, writing and analytics) or "publish"."""
    from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError

    from ..analytics.validation import ValidationError
    from ..db.r2 import R2Error

    if isinstance(err, (requests.ConnectionError, requests.Timeout)):
        return Outcome("retry", "network")
    if isinstance(err, ESPNInvalidLeague):
        return Outcome("missing", "provider_not_found")
    status = _status(err) if isinstance(err, (ESPNUnknownError, RuntimeError)) else None
    if isinstance(err, ESPNAccessDenied) or status in (401, 403):
        return Outcome("auth", "provider_auth") if authenticated else Outcome("private", "provider_private")
    if status == 429:
        return Outcome("retry", "provider_429", pause_provider=True)
    if status is not None and status >= 500:
        return Outcome("retry", "provider_5xx")
    if isinstance(err, R2Error):
        return Outcome("retry", "storage")
    try:
        import psycopg
        if isinstance(err, psycopg.OperationalError):
            return Outcome("retry", "control_plane")
    except ImportError:   # pragma: no cover - psycopg is a dependency of the sync group
        pass
    if isinstance(err, ValidationError):
        return Outcome("dead", "validation_error")
    if isinstance(err, (KeyError, TypeError, ValueError, IndexError)):
        return Outcome("retry", "provider_response") if stage == "fetch" else Outcome("dead", "parse_error")
    return Outcome("retry", "unexpected")

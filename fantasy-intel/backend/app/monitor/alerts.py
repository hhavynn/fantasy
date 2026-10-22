"""Alert states from usage samples and dead sync jobs: a pure function (plan Decisions, "Alerts" and "Dead jobs").

There are two alert kinds, each one GitHub issue: "Usage above 70%" (any metric with a quota at or above 70% of it)
and "Dead sync jobs" (any `dead` job in the last 7 days). Each state carries the set of keys that alert (metric
names, or job ids); github.py comments only when that set gains a member. Every string that reaches an issue is
checked against a strict shape here first: ids, short codes and numbers only, never a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from .collect import Sample

THRESHOLD = 0.70
USAGE_TITLE = "Usage above 70%"
DEAD_TITLE = "Dead sync jobs"
DEAD_WINDOW_DAYS = 7

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LEAGUE = re.compile(r"^lg_[A-Za-z0-9_-]{16,64}$")
_CODE = re.compile(r"^[a-z0-9_:.-]{1,64}$")
_METRIC = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True)
class DeadJob:
    job_id: str
    league_id: str
    kind: str
    error_code: str
    finished_at: str


@dataclass(frozen=True)
class AlertState:
    kind: str                    # usage | dead_jobs
    title: str
    active: bool
    keys: tuple[str, ...]        # what alerts: metric names or job ids, sorted
    known: bool                  # False: an input could not be read, so an absent key proves nothing
    body: str                    # public-safe markdown, without the state marker
    details: dict                # key -> one public-safe line, for comments


def _safe(value, pattern: re.Pattern, fallback: str) -> str:
    text = "" if value is None else str(value)
    return text if pattern.match(text) else fallback


def dead_job_from_row(row: Sequence) -> DeadJob:
    """A fi.dead_jobs() row (job_id, league_id, kind, error_code, finished_at), every field shape-checked."""
    job_id, league_id, kind, error_code, finished = row
    when = finished.strftime("%Y-%m-%dT%H:%MZ") if isinstance(finished, datetime) else ""
    return DeadJob(_safe(job_id, _UUID, "invalid"), _safe(league_id, _LEAGUE, "invalid"),
                   _safe(kind, _CODE, "other"), _safe(error_code, _CODE, "other") if error_code else "none", when)


def is_alerting(sample: Sample) -> bool:
    """At or above 70% of its quota. Rounded to 6 places, so 70/100 is exactly the threshold."""
    ratio = sample.ratio
    return ratio is not None and round(ratio, 6) >= THRESHOLD


def fmt_number(x: float) -> str:
    return f"{int(x):,}" if float(x).is_integer() else f"{x:,.3f}"


def fmt_percent(sample: Sample) -> str:
    return "" if sample.ratio is None else f"{sample.ratio * 100:.1f}%"


def _usage_state(samples: Iterable[Sample], known: bool) -> AlertState:
    hot = sorted((s for s in samples if _METRIC.match(s.metric) and is_alerting(s)), key=lambda s: s.metric)
    details = {s.metric: f"`{s.metric}` {fmt_number(s.used)} of {fmt_number(s.quota)} {s.unit} ({fmt_percent(s)})"
               + (" (injected)" if s.injected else "") for s in hot}
    if hot:
        rows = "\n".join(f"| `{s.metric}` | {fmt_number(s.used)} | {fmt_number(s.quota)} | {s.unit} | {fmt_percent(s)}"
                         f"{' (injected)' if s.injected else ''} |" for s in hot)
        body = ("These free-tier metrics are at or above 70% of their quota.\n\n"
                "| Metric | Used | Quota | Unit | % |\n|---|---|---|---|---|\n" + rows)
    else:
        body = "No metric is at or above 70% of its quota."
    return AlertState("usage", USAGE_TITLE, bool(hot), tuple(s.metric for s in hot), known, body, details)


def _dead_state(jobs: Sequence[DeadJob] | None) -> AlertState:
    if jobs is None:
        return AlertState("dead_jobs", DEAD_TITLE, False, (), False, "The dead-job check could not run.", {})
    jobs = sorted({j.job_id: j for j in jobs if j.job_id != "invalid"}.values(), key=lambda j: j.job_id)
    details = {j.job_id: f"`{j.job_id}` league `{j.league_id}` kind `{j.kind}` error `{j.error_code}`" for j in jobs}
    if jobs:
        rows = "\n".join(f"| `{j.job_id}` | `{j.league_id}` | {j.kind} | `{j.error_code}` | {j.finished_at} |"
                         for j in jobs)
        body = (f"Sync jobs that reached `dead` in the last {DEAD_WINDOW_DAYS} days.\n\n"
                "| Job | League | Kind | Error code | Finished (UTC) |\n|---|---|---|---|---|\n" + rows)
    else:
        body = f"No sync job reached `dead` in the last {DEAD_WINDOW_DAYS} days."
    return AlertState("dead_jobs", DEAD_TITLE, bool(jobs), tuple(j.job_id for j in jobs), True, body, details)


def evaluate(samples: Iterable[Sample], dead_jobs: Sequence[DeadJob] | None, *,
             usage_known: bool = True) -> list[AlertState]:
    """One state per alert kind. `dead_jobs` None: the check could not run. `usage_known` False: a usage source was
    unavailable this run, so a metric missing from `samples` may still be over its threshold."""
    return [_usage_state(list(samples), usage_known), _dead_state(dead_jobs)]

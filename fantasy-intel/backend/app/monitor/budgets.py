"""Explicit page, API and sync budgets (issue #201), next to the free-tier quotas the platform monitor alerts on.

Page budgets are for the backend fan-out a page waits on (every API call it makes, in the waves the page makes them),
measured in-process by scripts/perf_baseline.py on synthetic leagues; the Next.js render on top is small. They are
targets, not alerts: the monitor (collect.py, alerts.py) already reports quota pressure at 70 %.

  cold    the instance holds none of the league's files yet: the first visit after a cold start or a new publish
  warm    files cached, answers computed (the response cache is empty)
  cached  answers served from the per-instance response cache

The guardrails that keep a growing number of leagues inside the free tiers are in the request path, not here:
the per-instance file cache (bounded by bytes, one download per object), the budgeted degraded read and the failed-
fetch back-off (app/db/league_files.py), and response single flight (app/api/league_scope.py).
"""

from __future__ import annotations

from ..db.league_files import DEFAULT_COLD_BUDGET_S, FAILED_FETCH_RETRY_S
from . import collect

PAGE_BUDGET_MS = {"cold": 4000.0, "warm": 1500.0, "cached": 150.0}
# The heavy multi-week trade search (Trades -> Discover) streams under its header, so it has its own, looser budget.
TRADE_SCAN_BUDGET_MS = {"cold": 10000.0, "warm": 8000.0, "cached": 150.0}
COLD_LOAD_BUDGET_S = DEFAULT_COLD_BUDGET_S
FAILED_FETCH_BACKOFF_S = FAILED_FETCH_RETRY_S
# r2.download: objects of 16 MiB or more are fetched as 8 parallel ranged GETs (each a Class B operation).
_PARALLEL_FROM_BYTES, _PARTS = 16 << 20, 8


def page_budget_ms(page: str, kind: str) -> float:
    return (TRADE_SCAN_BUDGET_MS if page == "trades-discover" else PAGE_BUDGET_MS)[kind]


def within_budget(page: str, kind: str, ms: float) -> bool:
    return ms <= page_budget_ms(page, kind)


def r2_gets_per_download(nbytes: int, *, retried: bool = False) -> int:
    """Class B operations one cold download of an object costs (a retried transient failure doubles it at most)."""
    gets = _PARTS if nbytes >= _PARALLEL_FROM_BYTES else 1
    return gets * (2 if retried else 1)


class _NoRows:
    def execute(self, *_a):
        return self

    def fetchone(self):
        return (0, 0)

    def commit(self):
        pass


def free_tier_quotas() -> dict[str, tuple[float, str]]:
    """metric -> (monthly quota, unit), read from the monitor's own collectors so the two never disagree."""
    empty_ops = b'{"data":{"viewer":{"accounts":[{"r2OperationsAdaptiveGroups":[]}]}}}'
    samples = [*collect.parse_r2_operations(200, empty_ops), *collect.collect_r2(lambda: []).samples,
               *collect.collect_postgres(_NoRows()).samples]
    out = {s.metric: (float(s.quota), s.unit) for s in samples if s.quota}
    for metric, quota, unit, _ in collect.VERCEL_SERVICES.values():
        out[metric] = (float(quota), unit)
    return out
